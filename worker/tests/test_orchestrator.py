"""Tests for the plain-async orchestrator: planner gates, composer envelopes,
dashboard builder provenance, state expansion and end-to-end turns.

Flow tests run against the real DB + Redis (per AGENTS.md); planner/builder
tests are pure and never call an LLM (their prompts are monkeypatched).
"""

import json
import time

import httpx
import pytest
from conftest import make_ctx
from conftest import run as _run
from db_service import db
from redis_service import redis

from worker.agent import load_state, process_job
from worker.agents.base import AgentResult
from worker.agents.chat_agent import ChatAgent
from worker.agents.registry import get_subagent, list_subagents
from worker.agents.swot_agent import SwotAgent
from worker.helpers.messages import format_transcript, questionnaire_pending
from worker.helpers.persistence import add_message, create_dashboard, expand_dashboards
from worker.orchestrator import Orchestrator
from worker.orchestrator.composer import Composer, assistant_card, user_echo
from worker.orchestrator.dashboard_builder import (
    DashboardBuilder,
    _sections,
    _suggestions,
)
from worker.orchestrator.planner import Planner

TEST_EMAIL = "orchestrator-test@example.com"
TEST_IDEA = "I want to open a specialty coffee shop in Pune."


async def _cleanup(session_id=None):
    if session_id:
        await redis.delete(f"langgraph_state:{session_id}")
        await db.message.delete_many(where={"sessionId": session_id})
        await db.session.delete_many(where={"id": session_id})
    await db.user.delete_many(where={"email": TEST_EMAIL})


async def _subscribe(session_id):
    ps = redis.pubsub()
    await ps.subscribe(f"stream:{session_id}")
    await ps.get_message(timeout=1)
    return ps


async def _collect(ps, count: int, timeout: float = 10.0) -> list[dict]:
    events = []
    deadline = time.time() + timeout
    while len(events) < count and time.time() < deadline:
        msg = await ps.get_message(timeout=1)
        if msg and msg.get("type") == "message":
            events.append(json.loads(msg["data"]))
    return events


async def _make_session():
    await _cleanup()
    user = await db.user.create(data={"email": TEST_EMAIL, "name": "Test User"})
    session = await db.session.create(
        data={"userId": user.id, "business_idea": TEST_IDEA}
    )
    return session


async def _seed_completed_questionnaire(sid):
    await add_message(sid, "USER", "CHAT", {"type": "chat", "content": "seed"})
    await add_message(
        sid,
        "ASSISTANT",
        "TOOL",
        {
            "type": "questionnaire_complete",
            "content": "done",
            "context": {"business_about": TEST_IDEA},
        },
    )


class _NoLLMPlan:
    """_llm_plan stand-in that fails the test if the planner calls the LLM on a
    path that must be decided deterministically."""

    def __init__(self):
        self.called = False

    async def __call__(self, *args, **kwargs):
        self.called = True
        raise AssertionError("planner must not call the LLM here")


def _planner():
    return Planner.__new__(Planner)  # skip LLM construction


# ── planner: deterministic gates ───────────────────────────────


def test_pending_questionnaire_routes_without_llm():
    planner = _planner()
    llm = _NoLLMPlan()
    planner._llm_plan = llm
    messages = [
        {"role": "ASSISTANT", "agent": "TOOL", "type": "questionnaire", "content": "q",
         "questions": [{"key": "q1", "question": "who?"}]},
    ]
    plan = _run(planner.plan("sure, whatever", messages))
    assert plan.mode == "questionnaire"
    assert llm.called is False


def test_business_idea_plans_questionnaire():
    plan = _planner()._sanitize({"mode": "questionnaire"}, ready=False)
    assert plan.mode == "questionnaire"


def test_questionnaire_after_complete_degrades_to_chat():
    plan = _planner()._sanitize({"mode": "questionnaire"}, ready=True)
    assert plan.mode == "chat"
    assert plan.subagents == []


def test_gated_inline_redirects_to_questionnaire_when_incomplete():
    plan = _planner()._sanitize(
        {"mode": "inline", "subagents": [{"name": "swot", "query": "do swot"}]},
        ready=False,
    )
    assert plan.mode == "questionnaire"


def test_ungated_inline_runs_before_questionnaire():
    plan = _planner()._sanitize(
        {"mode": "inline", "subagents": [{"name": "indian_legal_search", "query": "fssai"}]},
        ready=False,
    )
    assert plan.mode == "inline"
    assert plan.subagents[0]["name"] == "indian_legal_search"


def test_inline_unknown_subagent_falls_back_to_chat():
    plan = _planner()._sanitize(
        {"mode": "inline", "subagents": [{"name": "does_not_exist", "query": "x"}]},
        ready=False,
    )
    assert plan.mode == "chat"


def test_inline_keeps_at_most_one_subagent():
    plan = _planner()._sanitize(
        {
            "mode": "inline",
            "subagents": [
                {"name": "finance", "query": "a"},
                {"name": "astrology", "query": "b"},
            ],
        },
        ready=True,
    )
    assert [s["name"] for s in plan.subagents] == ["finance"]


def test_dashboard_redirects_to_questionnaire_when_incomplete():
    plan = _planner()._sanitize(
        {"mode": "dashboard", "dashboard_kind": "swot",
         "subagents": [{"name": "swot", "query": "s"}]},
        ready=False,
    )
    assert plan.mode == "questionnaire"


def test_dashboard_keeps_kind_and_subagents_when_ready():
    plan = _planner()._sanitize(
        {"mode": "dashboard", "dashboard_kind": "swot",
         "subagents": [{"name": "swot", "query": "s"}]},
        ready=True,
    )
    assert plan.mode == "dashboard"
    assert plan.dashboard_kind == "swot"
    assert plan.subagents == [{"name": "swot", "query": "s"}]


def test_dashboard_unknown_kind_falls_back_to_general():
    plan = _planner()._sanitize(
        {"mode": "dashboard", "dashboard_kind": "banana",
         "subagents": [{"name": "swot", "query": "s"}]},
        ready=True,
    )
    assert plan.dashboard_kind == "general"


def test_dashboard_without_subagents_falls_back_to_chat():
    plan = _planner()._sanitize(
        {"mode": "dashboard", "dashboard_kind": "swot", "subagents": []},
        ready=True,
    )
    assert plan.mode == "chat"
    assert plan.dashboard_kind is None


def test_chat_drops_gated_support_before_completion():
    plan = _planner()._sanitize(
        {
            "mode": "chat",
            "subagents": [
                {"name": "web_search", "query": "q"},
                {"name": "indian_case_search", "query": "c"},
            ],
        },
        ready=False,
    )
    assert [s["name"] for s in plan.subagents] == ["indian_case_search"]


def test_questionnaire_never_appears_as_support_subagent():
    plan = _planner()._sanitize(
        {"mode": "chat", "subagents": [{"name": "questionnaire", "query": "q"}]},
        ready=True,
    )
    assert plan.subagents == []


def test_junk_mode_falls_back_to_chat():
    assert _planner()._sanitize({"mode": "whatever"}, ready=False).mode == "chat"


def test_llm_plan_failure_falls_back_to_chat():
    # The real _llm_plan swallows LLM/parse failures; a non-runnable model
    # makes the chain construction fail inside its try block.
    planner = _planner()
    planner.llm = object()
    plan = _run(planner.plan("hello", []))
    assert plan.mode == "chat"


def test_planner_prompt_declares_every_mode():
    from worker.prompts.orchestrator import PLAN_PROMPT

    for mode in ("chat", "questionnaire", "inline", "dashboard"):
        assert f'"{mode}"' in PLAN_PROMPT


# ── composer envelopes ─────────────────────────────────────────


def test_user_echo_shape():
    echo = user_echo("hi there")
    assert echo == {"role": "USER", "agent": "CHAT", "type": "chat", "content": "hi there"}


def test_assistant_card_spreads_data_as_siblings():
    result = AgentResult(
        text="summary",
        data={"query": "q", "results": [{"x": 1}], "disclaimer": "d"},
        message_type="legal_research",
    )
    card = assistant_card(result)
    assert card["role"] == "ASSISTANT"
    assert card["agent"] == "TOOL"
    assert card["type"] == "legal_research"
    assert card["content"] == "summary"
    assert card["query"] == "q"
    assert card["results"] == [{"x": 1}]
    assert card["disclaimer"] == "d"
    # data keys never collide with envelope keys
    assert "role" not in (result.data or {})


def test_run_subagent_unknown_name_returns_error_card():
    composer = Composer()
    ctx = make_ctx("hi", [])
    result = _run(composer.run_subagent("does_not_exist", "hi", ctx))
    assert result.message_type == "tool_error"


def test_run_subagent_failure_degrades_to_error_card(monkeypatch):
    async def boom(self, query, ctx):
        raise RuntimeError("kaput")

    monkeypatch.setattr(SwotAgent, "run", boom)
    result = _run(Composer().run_subagent("swot", "do it", make_ctx("do it", [])))
    assert result.message_type == "tool_error"
    assert "kaput" in result.text


def test_run_subagent_reraises_rate_limits(monkeypatch):
    response = httpx.Response(429, request=httpx.Request("POST", "https://x.test"))
    err = httpx.HTTPStatusError("429", request=response.request, response=response)

    async def limited(self, query, ctx):
        raise err

    monkeypatch.setattr(SwotAgent, "run", limited)
    with pytest.raises(httpx.HTTPStatusError):
        _run(Composer().run_subagent("swot", "do it", make_ctx("do it", [])))


def test_run_subagent_reraises_nested_rate_limits(monkeypatch):
    response = httpx.Response(429, request=httpx.Request("POST", "https://x.test"))
    nested = httpx.HTTPStatusError("429", request=response.request, response=response)

    async def limited(self, query, ctx):
        raise RuntimeError("wrapped") from nested

    monkeypatch.setattr(SwotAgent, "run", limited)
    with pytest.raises(httpx.HTTPStatusError):
        _run(Composer().run_subagent("swot", "do it", make_ctx("do it", [])))


# ── dashboard builder: provenance & sections (pure) ────────────


def test_suggestions_source_index_maps_to_real_source_only():
    sources = [{"label": "World Bank", "url": "https://worldbank.org/x"}]
    out = _suggestions(
        [
            {"text": "grow exports", "source_index": 0},
            {"text": "bad index", "source_index": 7},
            {"text": "bool index", "source_index": True},
            {"text": "null index", "source_index": None},
            {"text": "string index", "source_index": "0"},
        ],
        sources,
    )
    assert [s["origin"] for s in out] == ["sourced", "ai", "ai", "ai", "ai"]
    assert out[0]["source"]["url"] == "https://worldbank.org/x"
    assert all(s["source"] is None for s in out[1:])


def test_suggestions_without_sources_are_always_ai():
    out = _suggestions([{"text": "do this", "source_index": 0}], [])
    assert out == [{"text": "do this", "source": None, "origin": "ai"}]


def test_swot_sections_become_quadrants():
    result = AgentResult(
        text="md",
        data={"sections": {"strengths": ["a"], "weaknesses": [],
                           "opportunities": ["c"], "threats": ["d"]},
              "summary": "s"},
        message_type="swot",
    )
    sections = _sections([("swot", result)])
    assert [s["heading"] for s in sections] == [
        "Strengths", "Opportunities", "Threats",
    ]
    assert all(s["kind"] == "bullets" for s in sections)


def test_insights_become_bullets_and_text_falls_back():
    astro = AgentResult(text="x", data={"insights": ["i1", "i2"]},
                        message_type="astrology")
    legal = AgentResult(text="long text", message_type="legal_research")
    sections = _sections([("astrology", astro), ("legal", legal)])
    assert sections[0]["kind"] == "bullets"
    assert sections[0]["items"] == ["i1", "i2"]
    assert sections[1]["kind"] == "text"
    assert sections[1]["text"] == "long text"


def test_sections_deduplicate_empty_results():
    empty = AgentResult(text="", message_type="economics")
    assert _sections([("economics", empty)]) == []


def test_build_uses_narrative_and_validates_provenance(monkeypatch):
    async def fake_narrative(self, label, request, context, sections, sources):
        return {
            "name": "SWOT Analysis",
            "title": "Coffee shop SWOT",
            "subtitle": "Pune",
            "intro": "Dashboard ready!",
            "summary": "Solid niche.",
            "suggestions": [
                {"text": "Cite the source", "source_index": 0},
                {"text": "Invented index", "source_index": 9},
                {"text": "General advice", "source_index": None},
            ],
        }

    monkeypatch.setattr(DashboardBuilder, "_narrative", fake_narrative)
    swot = AgentResult(
        text="md",
        data={"sections": {"strengths": ["a"]}, "summary": "s"},
        message_type="swot",
        sources=[{"label": "A", "url": "https://a.example"}],
    )
    doc = _run(
        DashboardBuilder().build(
            kind="swot",
            request="swot please",
            context={"business_about": TEST_IDEA},
            results=[("swot", swot)],
        )
    )
    assert doc["kind"] == "swot"
    assert doc["kind_label"] == "SWOT Analysis"
    assert doc["name"] == "SWOT Analysis"
    assert doc["intro"] == "Dashboard ready!"
    assert doc["suggestions"][0]["origin"] == "sourced"
    assert doc["suggestions"][0]["source"]["url"] == "https://a.example"
    assert doc["suggestions"][1]["origin"] == "ai"  # out-of-range index refused
    assert doc["suggestions"][2]["origin"] == "ai"
    assert doc["sources"] == [{"label": "A", "url": "https://a.example"}]
    assert doc["sections"]  # deterministic draft survives alongside narrative


def test_build_fails_open_when_narrative_llm_dies(monkeypatch):
    async def boom(self, label, request, context, sections, sources):
        raise RuntimeError("llm down")

    monkeypatch.setattr(DashboardBuilder, "_narrative", boom)
    text = AgentResult(text="some findings", message_type="research")
    doc = _run(
        DashboardBuilder().build(
            kind="market_analysis",
            request="market?",
            context={},
            results=[("web_search", text)],
        )
    )
    assert doc["name"] == "Market Analysis"
    assert doc["intro"] == "Your Market Analysis dashboard is ready."
    assert doc["sections"][0]["text"] == "some findings"
    assert doc["suggestions"] == []


def test_narrative_rejects_suggestion_urls_outside_sources():
    # The prompt forbids hand-written URLs; anything that isn't a valid index
    # must never be presented as a citation.
    out = _suggestions(
        [{"text": "see https://invented.example/x", "source_index": None}],
        [],
    )
    assert out[0]["origin"] == "ai"
    assert out[0]["source"] is None


# ── transcript & state expansion ───────────────────────────────


def test_format_transcript_renders_dashboard_compactly():
    messages = [
        {
            "role": "ASSISTANT", "agent": "REPORT", "type": "dashboard",
            "content": "SWOT Analysis generated",
            "dashboard_name": "SWOT Analysis",
            "dashboard_data": {"summary": "Strong niche."},
        },
    ]
    assert format_transcript(messages) == (
        "ASSISTANT (dashboard): SWOT Analysis — Strong niche."
    )


def test_build_state_has_no_router_keys():
    import inspect

    from worker.helpers import persistence

    # _empty_state drives build_state_from_db; the router-era keys are gone.
    src = inspect.getsource(persistence._empty_state)
    assert '"intent"' not in src
    assert '"tool"' not in src


def test_expand_dashboards_resolves_bare_references():
    async def scenario():
        session = await _make_session()
        sid = session.id
        try:
            doc = {"kind": "swot", "summary": "from the row"}
            row = await create_dashboard(sid, "SWOT Analysis", doc)
            await add_message(sid, "ASSISTANT", "REPORT", {
                "type": "dashboard",
                "content": "SWOT Analysis generated",
                "dashboard_id": row.id,
                "dashboard_name": "SWOT Analysis",
            })
            messages = [
                {"role": "ASSISTANT", "agent": "REPORT", "type": "dashboard",
                 "content": "SWOT Analysis generated", "dashboard_id": row.id,
                 "dashboard_name": "SWOT Analysis"},
                {"role": "USER", "agent": "CHAT", "type": "chat", "content": "hi"},
            ]
            expanded = await expand_dashboards(messages)
            assert expanded[0]["dashboard_data"] == doc
            assert expanded[1] == messages[1]
            # Already-expanded entries are left untouched (no refetch work).
            again = await expand_dashboards(expanded)
            assert again == expanded

            # A reference without a row stays bare instead of crashing.
            orphan = [{"role": "ASSISTANT", "agent": "REPORT", "type": "dashboard",
                       "content": "x", "dashboard_id": "missing", "dashboard_name": "x"}]
            assert (await expand_dashboards(orphan))[0] == orphan[0]
        finally:
            await _cleanup(sid)

    _run(scenario())


# ── end-to-end turns (real DB + Redis) ─────────────────────────


def test_dashboard_turn_creates_row_and_streams_in_order(monkeypatch):
    """A dashboard turn: chat intro → dashboard frame → suggestions → end; the
    row is persisted, the DB entry keeps a reference only, and the in-memory
    state carries the expanded document."""

    async def fake_llm_plan(self, user_input, messages, ready):
        return {
            "mode": "dashboard",
            "dashboard_kind": "swot",
            "subagents": [{"name": "swot", "query": "SWOT for my business"}],
            "reply_instruction": "prep",
        }

    async def fake_swot(self, query, ctx):
        return AgentResult(
            text="### Strengths\n- loyal customers",
            data={"sections": {"strengths": ["loyal customers"]}, "summary": "ok"},
            message_type="swot",
            sources=[{"label": "Local news", "url": "https://news.example/coffee"}],
        )

    async def fake_narrative(self, label, request, context, sections, sources):
        return {
            "name": "Coffee SWOT",
            "title": "Coffee shop SWOT",
            "subtitle": "Pune",
            "intro": "Your SWOT dashboard is ready.",
            "summary": "A solid niche with room to grow.",
            "suggestions": [
                {"text": "Lean into loyalty", "source_index": 0},
                {"text": "Keep costs lean", "source_index": None},
            ],
        }

    monkeypatch.setattr(Planner, "_llm_plan", fake_llm_plan)
    monkeypatch.setattr(SwotAgent, "run", fake_swot)
    monkeypatch.setattr(DashboardBuilder, "_narrative", fake_narrative)

    async def scenario():
        session = await _make_session()
        sid = session.id
        await _seed_completed_questionnaire(sid)
        engine = Orchestrator()
        ps = await _subscribe(sid)
        try:
            result = await process_job(
                {"session_id": sid, "user_input": "Give me a SWOT dashboard"}, engine
            )
            events = await _collect(ps, 4)

            # Frame order: chat intro → dashboard → suggestions → end.
            assert [e["type"] for e in events] == [
                "chat", "dashboard", "suggestions", "end",
            ]
            assert events[0]["content"] == "Your SWOT dashboard is ready."
            assert events[1]["dashboard_name"] == "Coffee SWOT"

            dashboard_events = [
                m for m in result["messages"] if m["type"] == "dashboard"
            ]
            assert len(dashboard_events) == 1
            entry = dashboard_events[0]
            assert entry["role"] == "ASSISTANT"
            assert entry["agent"] == "REPORT"
            assert entry["dashboard_name"] == "Coffee SWOT"
            # The state keeps the expanded document for this session.
            assert entry["dashboard_data"]["kind"] == "swot"
            assert entry["dashboard_data"]["suggestions"][0]["origin"] == "sourced"
            assert entry["dashboard_data"]["suggestions"][1]["origin"] == "ai"

            # The USER echo precedes the intro (chat turn shape).
            types = [m["type"] for m in result["messages"]]
            assert types[-3:] == ["chat", "chat", "dashboard"]

            # The row exists and the stored message keeps a reference only.
            rows = await db.dashboard.find_many(where={"sessionId": sid})
            assert len(rows) == 1
            assert rows[0].name == "Coffee SWOT"
            assert rows[0].data["summary"] == "A solid niche with room to grow."

            stored = [
                m
                for m in await db.message.find_many(where={"sessionId": sid})
                if (m.content or {}).get("type") == "dashboard"
            ]
            assert len(stored) == 1
            assert stored[0].content["dashboard_id"] == rows[0].id
            assert "dashboard_data" not in stored[0].content
            assert stored[0].agent == "REPORT"
            assert stored[0].role == "ASSISTANT"

            # A cache miss rebuilds from the DB and re-expands the document.
            await redis.delete(f"langgraph_state:{sid}")
            reloaded = await load_state(sid)
            rebuilt = next(
                m for m in reloaded["messages"] if m["type"] == "dashboard"
            )
            assert rebuilt["dashboard_data"]["summary"] == (
                "A solid niche with room to grow."
            )

            await ps.unsubscribe(f"stream:{sid}")
            await ps.close()
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_dashboard_turn_fails_to_chat_when_all_feeds_fail(monkeypatch):
    async def fake_llm_plan(self, user_input, messages, ready):
        return {
            "mode": "dashboard",
            "dashboard_kind": "swot",
            "subagents": [{"name": "swot", "query": "SWOT"}],
        }

    async def fake_swot(self, query, ctx):
        raise RuntimeError("feed down")

    monkeypatch.setattr(Planner, "_llm_plan", fake_llm_plan)
    monkeypatch.setattr(SwotAgent, "run", fake_swot)

    async def scenario():
        session = await _make_session()
        sid = session.id
        await _seed_completed_questionnaire(sid)
        engine = Orchestrator()
        ps = await _subscribe(sid)
        try:
            result = await process_job(
                {"session_id": sid, "user_input": "dashboard please"}, engine
            )
            await _collect(ps, 3)

            types = [m["type"] for m in result["messages"]]
            assert "dashboard" not in types
            assert result["messages"][-1]["type"] == "chat"
            assert "couldn't gather" in result["messages"][-1]["content"]
            assert await db.dashboard.find_many(where={"sessionId": sid}) == []
            assert questionnaire_pending(result["messages"]) is False
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_chat_turn_passes_support_findings_to_chat_agent(monkeypatch):
    async def fake_llm_plan(self, user_input, messages, ready):
        return {
            "mode": "chat",
            "subagents": [{"name": "indian_legal_search", "query": "fssai licence"}],
            "reply_instruction": "Mention the licence requirement.",
        }

    async def fake_search(self, query, ctx):
        return AgentResult(
            text="FSSAI licence is mandatory.",
            data={"query": query, "results": [], "disclaimer": "not advice"},
            message_type="legal_research",
        )

    captured = {}

    async def fake_chat(self, user_input, transcript, context, tools, notes=""):
        captured["notes"] = notes
        return "You'll need an FSSAI licence."

    monkeypatch.setattr(Planner, "_llm_plan", fake_llm_plan)
    from worker.agents import legal_agents

    monkeypatch.setattr(legal_agents.IndianLegalSearchAgent, "run", fake_search)
    monkeypatch.setattr(ChatAgent, "run", fake_chat)

    async def scenario():
        session = await _make_session()
        sid = session.id
        engine = Orchestrator()
        ps = await _subscribe(sid)
        try:
            result = await process_job(
                {"session_id": sid, "user_input": "do I need a licence?"}, engine
            )
            events = await _collect(ps, 2)

            assert "FSSAI licence is mandatory." in captured["notes"]
            assert "Reply guidance" in captured["notes"]
            assert "Mention the licence requirement." in captured["notes"]

            # Plain chat turn: echo + reply, streamed chat → end, no suggestions
            # (questionnaire not complete yet).
            assert [e["type"] for e in events] == ["chat", "end"]
            assert [m["type"] for m in result["messages"]] == ["chat", "chat"]
            assert result["messages"][1]["content"] == "You'll need an FSSAI licence."
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_inline_turn_streams_single_card_and_user_echo(monkeypatch):
    async def fake_llm_plan(self, user_input, messages, ready):
        return {
            "mode": "inline",
            "subagents": [{"name": "indian_case_search", "query": "find label cases"}],
        }

    async def fake_cases(self, query, ctx):
        return AgentResult(
            text="One case found.",
            data={"query": query, "cases": [{"case_name": "A v B"}], "disclaimer": "db"},
            message_type="case_search",
        )

    monkeypatch.setattr(Planner, "_llm_plan", fake_llm_plan)
    from worker.agents import legal_agents

    monkeypatch.setattr(legal_agents.IndianCaseSearchAgent, "run", fake_cases)

    async def scenario():
        session = await _make_session()
        sid = session.id
        engine = Orchestrator()
        ps = await _subscribe(sid)
        try:
            result = await process_job(
                {"session_id": sid, "user_input": "any label cases?"}, engine
            )
            events = await _collect(ps, 2)

            # The USER echo is persisted (chat-shaped) but never streamed.
            assert [e["type"] for e in events] == ["case_search", "end"]
            echo, card = result["messages"][-2], result["messages"][-1]
            assert echo == user_echo("any label cases?")
            assert card["type"] == "case_search"
            assert card["cases"] == [{"case_name": "A v B"}]

            stored = await db.message.find_many(where={"sessionId": sid})
            assert [m.role for m in stored] == ["USER", "ASSISTANT"]
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_suggestions_list_matches_subagent_registry():
    """The suggestions frame keeps the old `tools` key and lists every
    registered subagent except the questionnaire once context exists."""
    names = {t["name"] for t in list_subagents()}
    assert names == {
        "questionnaire", "swot", "web_search", "economics", "foresight",
        "finance", "astrology", "indian_legal_search", "indian_case_search",
        "legal_issue_register", "indian_finance",
    }
    assert get_subagent("swot").requires_context is True
    assert get_subagent("indian_legal_search").requires_context is False
