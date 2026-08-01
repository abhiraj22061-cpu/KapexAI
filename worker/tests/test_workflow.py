import asyncio
import builtins
import json
import time

import pytest

from db_service import connect_db, disconnect_db, db
from redis_service import pop_message, publish, push_message, redis

from worker.agent import build_graph, State
from worker.agents.questionnaire_agent import USER_QUEUE, QuestionnaireAgent
from worker.helpers.events import EVENTS_CHANNEL

TEST_EMAIL = "workflow-test@example.com"

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


@pytest.fixture(scope="session", autouse=True)
def _close_shared_loop():
    yield
    _loop.run_until_complete(_loop.shutdown_asyncgens())
    _loop.close()


def _feed(answers):
    inputs = iter(answers)

    def _input(*_args):
        try:
            return next(inputs)
        except StopIteration:
            raise SystemExit("quit") from None

    builtins.input = _input


async def _collect_events(ps, count: int, timeout: float = 15.0) -> list[dict]:
    events = []
    deadline = time.time() + timeout
    while len(events) < count and time.time() < deadline:
        msg = await ps.get_message(timeout=1)
        if msg and msg.get("type") == "message":
            events.append(json.loads(msg["data"]))
    return events


def test_redis_queue_and_pubsub():
    async def scenario():
        await redis.delete("test_queue", "test_channel")

        await push_message("test_queue", "hello")
        assert await pop_message("test_queue") == "hello"

        ps = redis.pubsub()
        await ps.subscribe("test_channel")
        await ps.get_message(timeout=5)

        await publish("test_channel", "payload")
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

    monkeypatch.setattr(
        agent_mod.research_agent,
        "run",
        lambda answers: "FAKE RESEARCH: the coffee market in Pune is growing.",
    )
    monkeypatch.setattr(
        agent_mod.report_agent,
        "run",
        lambda result: "FAKE REPORT:\n## Executive Summary\n- strong opportunity",
    )

    async def scenario():
        await connect_db()
        try:
            await db.user.delete_many(where={"email": TEST_EMAIL})
            ps = redis.pubsub()
            await ps.subscribe(EVENTS_CHANNEL)
            await ps.get_message(timeout=5)

            _feed([TEST_EMAIL, "Bob", "Coffee shop in Pune", "Pune", "5 stores in 5 years"])
            result = await build_graph().ainvoke(State())

            assert result["user_name"] == "Bob"
            assert result["report"] == "FAKE REPORT:\n## Executive Summary\n- strong opportunity"

            user = await db.user.find_first(where={"email": TEST_EMAIL})
            assert user is not None and user.name == "Bob"

            session = await db.session.find_first(
                where={"userId": user.id}, order={"created_at": "desc"}
            )
            assert session is not None
            assert session.business_idea == "Coffee shop in Pune"

            msgs = await db.message.find_many(
                where={"sessionId": session.id}, order={"created_at": "asc"}
            )
            agents = [m.agent for m in msgs]
            assert agents.count("QUESTIONNAIRE") == 4
            assert "RESEARCH" in agents
            assert "REPORT" in agents
            assert "GUARDRAIL" in agents

            queued = await redis.lrange(USER_QUEUE, 0, -1)
            mine = [e for e in queued if f'"session_id": "{session.id}"' in e]
            assert len(mine) == 5
            for entry in mine:
                await redis.lrem(USER_QUEUE, 0, entry)

            events = await _collect_events(ps, 9)
            types = {e["type"] for e in events}
            assert types == {
                "session_started",
                "answer_received",
                "questionnaire_complete",
                "research_complete",
                "report_complete",
                "guardrail_complete",
            }
            final = [e for e in events if e["type"] == "guardrail_complete"][0]
            assert "report" in final and "guardrail" in final

            await ps.unsubscribe(EVENTS_CHANNEL)
            await ps.close()
        finally:
                await db.user.delete_many(where={"email": TEST_EMAIL})
                await disconnect_db()

    _run(scenario())


def test_resume_skips_answered_questions(monkeypatch):
    from worker import agent as agent_mod

    monkeypatch.setattr(agent_mod.research_agent, "run", lambda answers: "FAKE RESEARCH")
    monkeypatch.setattr(agent_mod.report_agent, "run", lambda result: "FAKE REPORT")

    async def scenario():
        await connect_db()
        try:
            await db.user.delete_many(where={"email": TEST_EMAIL})
            first = QuestionnaireAgent()
            _feed([TEST_EMAIL, "Bob", "Coffee shop in Pune"])
            with pytest.raises(SystemExit):
                await first.ask()

            user = await db.user.find_first(where={"email": TEST_EMAIL})
            session = await db.session.find_first(
                where={"userId": user.id}, order={"created_at": "desc"}
            )
            before = len(
                await db.message.find_many(where={"sessionId": session.id})
            )

            agent_mod.questionnaire_agent.answers = {}
            agent_mod.questionnaire_agent.session_id = None
            _feed([TEST_EMAIL, "Pune", "5 stores in 5 years"])
            await build_graph().ainvoke(State())

            qa = agent_mod.questionnaire_agent
            assert qa.answers["user_name"] == "Bob"
            assert qa.answers["business_about"] == "Coffee shop in Pune"
            assert qa.answers["business_location"] == "Pune"
            assert qa.answers["business_vision"] == "5 stores in 5 years"
            assert qa.session_id == session.id

            msgs = await db.message.find_many(
                where={"sessionId": session.id}, order={"created_at": "asc"}
            )
            user_msgs = [m for m in msgs if m.role == "USER"]
            assert len(user_msgs) == 4
            assert len(msgs) - before == 5
        finally:
            await db.user.delete_many(where={"email": TEST_EMAIL})
            await disconnect_db()

    _run(scenario())
