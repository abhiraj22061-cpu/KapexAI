import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field
from redis_service import push_message

from worker.agents.guardrail_agent import GuardrailAgent
from worker.agents.questionnaire_agent import QuestionnaireAgent, USER_QUEUE
from worker.agents.report_agent import ReportAgent
from worker.agents.research_agent import ResearchAgent
from worker.helpers.events import publish_event
from worker.helpers.persistence import add_message


class State(BaseModel):
    user_name: str = Field(default="", description="User's name")
    business_about: str = Field(default="", description="User's short description of the business")
    business_location: str = Field(default="", description="User's desired business location")
    business_vision: str = Field(default="", description="User's business vision / target scale and population")
    research_result: str = Field(default="", description="Research gathered by the research agent")
    report: str = Field(default="", description="Structured professional report")
    guardrail: dict[str, str] = Field(default_factory=dict, description="Guardrail agent output")


questionnaire_agent = QuestionnaireAgent()
research_agent = ResearchAgent()
report_agent = ReportAgent()
guardrail_agent = GuardrailAgent()


async def orchestrator(state: State) -> State:
    answers = await questionnaire_agent.ask()
    state.user_name = answers["user_name"]
    state.business_about = answers["business_about"]
    state.business_location = answers["business_location"]
    state.business_vision = answers["business_vision"]

    session_id = questionnaire_agent.session_id

    result = research_agent.run(answers)
    state.research_result = result
    await add_message(
        session_id,
        "ASSISTANT",
        "RESEARCH",
        {"type": "research", "content": result},
    )
    await publish_event("research_complete", session_id, summary=result)

    state.report = report_agent.run(result)
    await add_message(
        session_id,
        "ASSISTANT",
        "REPORT",
        {"type": "report", "content": state.report},
    )
    await push_message(
        USER_QUEUE,
        json.dumps(
            {
                "session_id": session_id,
                "key": "report",
                "content": state.report,
                "agent": "REPORT",
            }
        ),
    )
    await publish_event("report_complete", session_id, report=state.report)

    state.guardrail = guardrail_agent.run(state.report)
    await add_message(
        session_id,
        "ASSISTANT",
        "GUARDRAIL",
        {"type": "guardrail", "content": state.guardrail},
    )
    await publish_event(
        "guardrail_complete",
        session_id,
        report=state.report,
        guardrail=state.guardrail,
    )
    return state


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(State)
    graph.add_node("orchestrator", orchestrator)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()


async def run_cli():
    from db_service import connect_db, disconnect_db

    await connect_db()
    try:
        graph = build_graph()
        print("Agent ready. Type 'quit' at any prompt to exit.\n")
        result = await graph.ainvoke(State())
        print("\n" + "=" * 60)
        print("STRUCTURED REPORT")
        print("=" * 60)
        print(result["report"])
        print("=" * 60)
        print("Guardrail:", result["guardrail"]["message"])
    finally:
        await disconnect_db()


if __name__ == "__main__":
    asyncio.run(run_cli())
