"""Shared pytest fixtures for worker tests.

Workers tests run against the real database + Redis (per AGENTS.md), so the
connection lifecycle and the single event loop every coroutine in the suite
runs on are centralized here. Test modules must use `from conftest import
_loop, run as _run` instead of creating their own loop — the redis_service
client binds its connections to whichever loop created them, so a second loop
in the same process (e.g. a second test file) will collide with the first.
"""

import asyncio
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from db_service import connect_db, disconnect_db
from redis_service import connect_redis, disconnect_redis

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def services():
    run(connect_db())
    run(connect_redis())
    yield
    run(disconnect_redis())
    run(disconnect_db())
    run(_loop.shutdown_asyncgens())
    _loop.close()

def llm_plan_from(fake_classify):
    """Adapts an old-style `RouterAgent.classify` fake to `Planner._llm_plan`.
    The planner's deterministic gates (pending questionnaire, context-gating,
    questionnaire-after-complete, unknown-name drops) still run for real."""

    async def fake_llm_plan(self, user_input, messages, ready):
        decision = await fake_classify(None, user_input, messages, None)
        if isinstance(decision, dict) and decision.get("intent") == "tool" and decision.get("tool"):
            name = decision["tool"]
            if name == "questionnaire":
                return {"mode": "questionnaire", "subagents": []}
            return {
                "mode": "inline",
                "subagents": [{"name": name, "query": user_input}],
                "reply_instruction": "",
            }
        return {"mode": "chat", "subagents": []}

    return fake_llm_plan


def make_ctx(request="", messages=None, session_id="s", user_id=""):
    """Builds an AgentContext the way the orchestrator does (for unit tests
    that drive a subagent directly with an old-style state dict)."""
    from worker.agents.base import AgentContext
    from worker.helpers.messages import business_context, format_transcript

    messages = messages or []
    return AgentContext(
        session_id=session_id,
        user_id=user_id,
        user_input=request,
        messages=messages,
        business_context=business_context(messages),
        transcript=format_transcript(messages),
    )

