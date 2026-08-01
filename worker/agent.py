import asyncio
import json
import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from redis_service import redis

from worker.agents.guardrail_agent import GuardrailAgent
from worker.agents.questionnaire_agent import QuestionnaireAgent
from worker.agents.report_agent import ReportAgent
from worker.agents.research_agent import ResearchAgent
from worker.helpers.events import publish_stream
from worker.helpers.persistence import (
    add_message,
    build_state_from_db,
    get_session,
    mark_session_failed,
)

logger = logging.getLogger(__name__)

STATE_KEY = "langgraph_state:{session_id}"
STATE_TTL = 60 * 60 * 24  # 24 hours


class State(TypedDict):
    session_id: str
    user_id: str
    user_input: str
    answers: dict[str, str]
    questions: list[dict[str, str]]
    research_result: str
    report: str
    guardrail: dict[str, str]
    phase: str


questionnaire_agent = QuestionnaireAgent()
research_agent = ResearchAgent()
report_agent = ReportAgent()
guardrail_agent = GuardrailAgent()


async def orchestrator(state: State) -> dict:
    """Consumes the current user message during the questionnaire phase and
    returns only the fields it changed; the conditional edge then routes to the
    right agent (or END) based on the updated phase."""
    if state["phase"] != "QUESTIONNAIRE":
        return {}
    if not state.get("questions"):
        return await questionnaire_agent.start(state)
    return await questionnaire_agent.collect_answers(state)


async def research_node(state: State) -> dict:
    result = await asyncio.to_thread(research_agent.run, state["answers"])
    await publish_stream(state["session_id"], {"type": "research", "content": result})
    return {"research_result": result, "phase": "REPORT"}


async def report_node(state: State) -> dict:
    result = await asyncio.to_thread(report_agent.run, state["research_result"])
    await publish_stream(state["session_id"], {"type": "report", "content": result})
    return {"report": result, "phase": "GUARDRAIL"}


async def guardrail_node(state: State) -> dict:
    result = await asyncio.to_thread(guardrail_agent.run, state["report"])
    await add_message(
        state["session_id"],
        "ASSISTANT",
        "REPORT",
        {"type": "report", "content": state["report"]},
    )
    await publish_stream(state["session_id"], {"type": "guardrail", "content": result})
    await publish_stream(state["session_id"], {"type": "end"})
    return {"guardrail": result, "phase": "COMPLETE"}


def route(state: State) -> str:
    phase = state["phase"]
    if phase == "RESEARCH":
        return "research"
    if phase == "REPORT":
        return "report"
    if phase == "GUARDRAIL":
        return "guardrail"
    return END


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(State)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("research", research_node)
    graph.add_node("report", report_node)
    graph.add_node("guardrail", guardrail_node)

    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges(
        "orchestrator",
        route,
        {"research": "research", "report": "report", "guardrail": "guardrail", END: END},
    )
    graph.add_edge("research", "report")
    graph.add_edge("report", "guardrail")
    graph.add_edge("guardrail", END)

    return graph.compile()


async def load_state(session_id: str) -> State:
    raw = await redis.get(STATE_KEY.format(session_id=session_id))
    if raw:
        state = json.loads(raw)
    else:
        session = await get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        state = await build_state_from_db(session)
    return state


async def save_state(session_id: str, state: State) -> None:
    await redis.set(
        STATE_KEY.format(session_id=session_id),
        json.dumps(state),
        ex=STATE_TTL,
    )


async def process_job(job: dict, graph: CompiledStateGraph) -> State:
    session_id = job["session_id"]
    job_id = str(job.get("job_id", "") or "")
    user_input = str(job.get("user_input", "") or "")
    try:
        state = await load_state(session_id)
        state["user_input"] = user_input
        result = await graph.ainvoke(state)
        await save_state(session_id, result)
        return result
    except Exception:
        logger.exception("Failed to process session %s (job %s)", session_id, job_id)
        try:
            await mark_session_failed(session_id)
            await publish_stream(
                session_id,
                {
                    "type": "error",
                    "job_id": job_id,
                    "content": f"Job {job_id} failed" if job_id else "Job failed",
                },
            )
        except Exception:
            logger.exception("Failed to notify error for session %s", session_id)
        raise
