"""Decides how to handle the user's latest message.

The planner is the orchestrator's policy layer: it runs deterministic gates
first (pending questionnaire, context-gated subagents, dashboards before
context exists), then asks the LLM for a plan and sanitizes it so the
executor can trust the result without re-checking anything.
"""

import json
import logging
from dataclasses import dataclass, field

from worker.agents.registry import get_subagent, list_subagents
from worker.helpers.json_utils import parse_json
from worker.helpers.messages import (
    business_context,
    format_transcript,
    questionnaire_complete,
    questionnaire_pending,
)
from worker.llm import get_llm
from worker.prompts.orchestrator import PLAN_TEMPLATE, STAGE_ONBOARDING, STAGE_READY

logger = logging.getLogger(__name__)

MODES = {"chat", "questionnaire", "inline", "dashboard"}

# Dashboard kinds the planner may name. Anything else (or a parse hiccup)
# falls back to the generic "general" preset, which renders any sections.
DASHBOARD_KINDS = {
    "swot",
    "market_analysis",
    "competitor_analysis",
    "financial_analysis",
    "risk_analysis",
    "scenario_analysis",
}
FALLBACK_KIND = "general"

# The dashboard turn never runs before context exists — dashboards analyze a
# real business, so they are redirected into the guided setup first.
QUESTIONNAIRE_MODE = "questionnaire"


@dataclass
class Plan:
    mode: str = "chat"
    subagents: list[dict] = field(default_factory=list)  # [{name, query}]
    dashboard_kind: str | None = None
    reply_instruction: str = ""


class Planner:
    def __init__(self) -> None:
        self.llm = get_llm(0.1)

    async def plan(self, user_input: str, messages: list[dict]) -> Plan:
        # Deterministic gate: while the interview is pending every message goes
        # back to the questionnaire (answers, structured slide payloads,
        # clarifications — the agent itself decides how to interpret it).
        if questionnaire_pending(messages):
            return Plan(mode=QUESTIONNAIRE_MODE)

        ready = questionnaire_complete(messages)
        raw = await self._llm_plan(user_input, messages, ready)
        return self._sanitize(raw, ready)

    async def _llm_plan(self, user_input: str, messages: list[dict], ready: bool) -> dict:
        try:
            chain = PLAN_TEMPLATE | self.llm
            response = await chain.ainvoke(
                {
                    "stage_rules": STAGE_READY if ready else STAGE_ONBOARDING,
                    "subagents": json.dumps(list_subagents(), indent=2),
                    "transcript": format_transcript(messages),
                    "context": json.dumps(business_context(messages), indent=2),
                    "user_input": user_input,
                }
            )
            data = parse_json(response.content)
        except Exception:
            logger.exception("Planner failed; falling back to chat")
            return {}
        return data if isinstance(data, dict) else {}

    def _sanitize(self, raw: dict, ready: bool) -> Plan:
        instruction = str(raw.get("reply_instruction") or "")
        mode = str(raw.get("mode") or "chat")
        if mode not in MODES:
            mode = "chat"

        kind = raw.get("dashboard_kind")
        kind = kind if isinstance(kind, str) and kind in DASHBOARD_KINDS else None

        subagents = self._valid_subagents(raw.get("subagents"))

        if mode == QUESTIONNAIRE_MODE:
            # Valid only before completion; once context exists the interview
            # is pointless, so a late selection degrades to conversation.
            return Plan(mode=mode if not ready else "chat", reply_instruction=instruction)

        if mode == "inline":
            subagents = subagents[:1]  # one card per turn
            if subagents and not ready and self._gated(subagents[0]["name"]):
                # Deterministic redirect: a context-requiring analysis before
                # the questionnaire completes starts the interview instead.
                return Plan(mode=QUESTIONNAIRE_MODE)
            if not subagents:
                mode = "chat"

        if mode == "dashboard":
            if not ready:
                return Plan(mode=QUESTIONNAIRE_MODE)
            if not subagents:
                mode = "chat"
                kind = None
            elif kind is None:
                kind = FALLBACK_KIND

        if not ready:
            # Support subagents that need the questionnaire's context are
            # dropped from plain chat turns (the reply simply lacks them).
            subagents = [s for s in subagents if not self._gated(s["name"])]

        return Plan(
            mode=mode,
            subagents=subagents,
            dashboard_kind=kind if mode == "dashboard" else None,
            reply_instruction=instruction,
        )

    @staticmethod
    def _gated(name: str) -> bool:
        agent = get_subagent(name)
        return bool(agent is not None and agent.requires_context)

    def _valid_subagents(self, raw: object) -> list[dict]:
        """Keeps only well-formed selections the registry knows about. The
        questionnaire never appears as a support/inline/dashboard subagent —
        it has its own deterministic mode."""
        out: list[dict] = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            agent = get_subagent(name)
            if agent is None or agent.name == "questionnaire":
                continue
            query = str(item.get("query") or "").strip() or agent.description
            out.append({"name": name, "query": query})
        return out
