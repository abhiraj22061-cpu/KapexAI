import asyncio
import json
import time

import pytest

from db_service import connect_db, disconnect_db, db
from redis_service import connect_redis, disconnect_redis, redis

from worker.agent import build_graph, load_state, process_job, save_state
from worker.helpers.persistence import build_state_from_db

TEST_EMAIL = "workflow-test@example.com"
TEST_IDEA = (
    "I want to open a specialty coffee shop in Pune, aiming for 5 stores in "
    "5 years, targeting young professionals."
)

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def _services():
    _run(connect_db())
    _run(connect_redis())
    yield
    _run(disconnect_redis())
    _run(disconnect_db())
    _run(_loop.shutdown_asyncgens())
    _loop.close()


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


def test_redis_queue_and_pubsub():
    async def scenario():
        await redis.delete("test_queue", "test_channel")

        await redis.rpush("test_queue", "hello")
        assert await redis.lpop("test_queue") == "hello"

        ps = redis.pubsub()
        await ps.subscribe("test_channel")
        await ps.get_message(timeout=5)

        await redis.publish("test_channel", "payload")
        received = None
        deadline = time.time() + 5
        while received is None and time.time() < deadline:
            msg = await ps.get_message(timeout=1)
            if msg and msg.get("type") == "message":
                received = msg["data"]
        assert received == "payload"

        await ps.unsubscribe("test_channel")
        await ps.close()

    _run(scenario())


def test_full_workflow_queue_and_pubsub(monkeypatch):
    from worker import agent as agent_mod

    async def fake_plan(idea):
        return {
            "facts": {"business_about": idea, "business_location": "Pune"},
            "questions": [
                {"key": "q1", "question": "Who is your target customer?"},
                {"key": "q2", "question": "How will you fund this?"},
            ],
        }

    async def fake_parse(questions, answers_text):
        return ["Young professionals in Pune", "Self-funded"]

    monkeypatch.setattr(agent_mod.questionnaire_agent, "_plan", fake_plan)
    monkeypatch.setattr(agent_mod.questionnaire_agent, "_parse", fake_parse)
    monkeypatch.setattr(
        agent_mod.research_agent, "run", lambda answers: "FAKE RESEARCH"
    )
    monkeypatch.setattr(
        agent_mod.report_agent, "run", lambda result: "FAKE REPORT"
    )

    async def scenario():
        session = await _make_session()
        sid = session.id
        graph = build_graph()
        ps = await _subscribe(sid)
        try:
            first = await process_job({"session_id": sid, "user_input": TEST_IDEA}, graph)
            events1 = await _collect(ps, 1)

            assert first["phase"] == "QUESTIONNAIRE"
            assert first["answers"]["business_about"] == TEST_IDEA
            assert first["answers"]["business_location"] == "Pune"
            assert len(first["questions"]) == 2
            assert events1[0]["type"] == "questionnaire"
            assert [q["key"] for q in events1[0]["questions"]] == ["q1", "q2"]

            second = await process_job(
                {"session_id": sid, "user_input": "Young professionals. Self-funded."},
                graph,
            )
            events2 = await _collect(ps, 5)

            assert second["phase"] == "COMPLETE"
            assert second["answers"]["q1"] == "Young professionals in Pune"
            assert second["research_result"] == "FAKE RESEARCH"
            assert second["report"] == "FAKE REPORT"
            assert second["guardrail"]["status"] == "not_implemented"

            types = [e["type"] for e in events2]
            assert types == [
                "questionnaire_complete",
                "research",
                "report",
                "guardrail",
                "end",
            ]

            msgs = await db.message.find_many(where={"sessionId": sid})
            assert sorted(m.agent for m in msgs) == [
                "QUESTIONNAIRE",
                "QUESTIONNAIRE",
                "REPORT",
            ]
            types = [m.content["type"] for m in msgs]
            assert sorted(types) == ["answers", "idea", "report"]
            report_msg = next(m for m in msgs if m.content["type"] == "report")
            assert report_msg.content["content"] == "FAKE REPORT"

            saved = json.loads(await redis.get(f"langgraph_state:{sid}"))
            assert saved["phase"] == "COMPLETE"
            assert saved["report"] == "FAKE REPORT"

            await ps.unsubscribe(f"stream:{sid}")
            await ps.close()
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_job_failure_publishes_error(monkeypatch):
    from worker import agent as agent_mod

    async def fake_plan(idea):
        return {
            "facts": {"business_about": idea, "business_location": "Pune"},
            "questions": [{"key": "q1", "question": "Who is your target customer?"}],
        }

    async def fake_parse(questions, answers_text):
        return ["Young professionals in Pune"]

    monkeypatch.setattr(agent_mod.questionnaire_agent, "_plan", fake_plan)
    monkeypatch.setattr(agent_mod.questionnaire_agent, "_parse", fake_parse)
    monkeypatch.setattr(
        agent_mod.research_agent,
        "run",
        lambda answers: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    async def scenario():
        session = await _make_session()
        sid = session.id
        ps = await _subscribe(sid)
        graph = build_graph()
        try:
            await process_job({"session_id": sid, "user_input": TEST_IDEA}, graph)
            events1 = await _collect(ps, 1)
            assert events1[0]["type"] == "questionnaire"

            with pytest.raises(RuntimeError):
                await process_job(
                    {"job_id": "job-123", "session_id": sid, "user_input": "answer"},
                    graph,
                )
            events2 = await _collect(ps, 2)

            types = [e["type"] for e in events2]
            assert types == ["questionnaire_complete", "error"]
            error = events2[-1]
            assert error["job_id"] == "job-123"
            assert "failed" in error["content"]

            failed_session = await db.session.find_unique(where={"id": sid})
            assert failed_session.status == "FAILED"

            await ps.unsubscribe(f"stream:{sid}")
            await ps.close()
        finally:
            await _cleanup(sid)

    _run(scenario())


def test_resume_from_db_history(monkeypatch):
    from worker import agent as agent_mod

    async def fake_plan(idea):
        return {
            "facts": {"business_about": idea, "business_location": "Pune"},
            "questions": [
                {"key": "q1", "question": "Who is your target customer?"},
                {"key": "q2", "question": "How will you fund this?"},
            ],
        }

    async def fake_parse(questions, answers_text):
        return ["Young professionals in Pune", "Self-funded"]

    monkeypatch.setattr(agent_mod.questionnaire_agent, "_plan", fake_plan)
    monkeypatch.setattr(agent_mod.questionnaire_agent, "_parse", fake_parse)
    monkeypatch.setattr(
        agent_mod.research_agent, "run", lambda answers: "FAKE RESEARCH"
    )
    monkeypatch.setattr(
        agent_mod.report_agent, "run", lambda result: "FAKE REPORT"
    )

    async def scenario():
        session = await _make_session()
        sid = session.id
        try:
            state = await build_state_from_db(session)
            assert state["phase"] == "QUESTIONNAIRE"
            assert state["answers"]["business_about"] == TEST_IDEA

            state.update(await agent_mod.questionnaire_agent.start(state))
            assert state["phase"] == "QUESTIONNAIRE"
            assert state["questions"]

            state.update(await agent_mod.questionnaire_agent.collect_answers(state))
            assert state["phase"] == "RESEARCH"
            assert state["answers"].get("q1")

            rebuilt = await build_state_from_db(session)
            assert rebuilt["phase"] == "RESEARCH"
            assert rebuilt["answers"] == state["answers"]
            assert rebuilt["questions"] == []

            await save_state(sid, state)
            loaded = await load_state(sid)
            assert loaded["phase"] == "RESEARCH"
            assert loaded["answers"] == state["answers"]
        finally:
            await _cleanup(sid)

    _run(scenario())
