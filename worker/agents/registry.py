from worker.agents.astrology_agent import AstrologyAgent
from worker.agents.base import SubAgent
from worker.agents.economics_agent import EconomicsAgent
from worker.agents.finance_agent import FinanceAgent
from worker.agents.foresight_agent import ForesightAgent
from worker.agents.indian_finance_agent import IndianFinanceAgent
from worker.agents.legal_agents import (
    IndianCaseSearchAgent,
    IndianLegalSearchAgent,
    LegalIssueRegisterAgent,
)
from worker.agents.questionnaire_agent import QuestionnaireAgent
from worker.agents.swot_agent import SwotAgent
from worker.agents.web_search_agent import WebSearchAgent

_REGISTRY: dict[str, SubAgent] = {}


def register(agent: SubAgent) -> None:
    _REGISTRY[agent.name] = agent


register(QuestionnaireAgent())
register(SwotAgent())
register(WebSearchAgent())
register(EconomicsAgent())
register(ForesightAgent())
register(FinanceAgent())
register(AstrologyAgent())
register(IndianLegalSearchAgent())
register(IndianCaseSearchAgent())
register(LegalIssueRegisterAgent())
register(IndianFinanceAgent())


def get_subagent(name: str) -> SubAgent | None:
    return _REGISTRY.get(name)


def list_subagents() -> list[dict]:
    """Serializable roster used by the planner prompt and the suggestions frame."""
    return [
        {
            "name": agent.name,
            "description": agent.description,
            "example": agent.example,
            "suggestion": agent.suggestion,
        }
        for agent in _REGISTRY.values()
    ]
