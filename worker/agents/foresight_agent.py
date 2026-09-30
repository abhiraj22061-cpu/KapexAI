import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from worker.llm import get_llm

from worker.helpers.http_cache import cached_json
from worker.helpers.json_utils import extract_text, parse_json
from worker.prompts.foresight import (
    FORESIGHT_PLAN_TEMPLATE,
    FORESIGHT_SUMMARY_TEMPLATE,
)
from worker.agents.base import AgentContext, AgentResult, SubAgent

JPL_SOURCE = "https://ssd.jpl.nasa.gov/api/horizons.api"

DISCLAIMER = (
    "Astronomical data is scientific; astrological interpretation is "
    "non-scientific and must not drive high-stakes decisions."
)


async def fetch_jpl_horizons_ephemeris(
    target: str,
    start_time: str,
    stop_time: str,
    step_size: str = "1 d",
    center: str = "500@399",
) -> dict:
    """Fetches astronomical ephemeris data from NASA JPL Horizons.

    Supplies astronomical positions, not evidence that astrology predicts
    personal or business outcomes.
    """
    payload = await cached_json(
        "GET",
        JPL_SOURCE,
        params={
            "format": "json",
            "COMMAND": f"'{target}'",
            "OBJ_DATA": "'YES'",
            "MAKE_EPHEM": "'YES'",
            "EPHEM_TYPE": "'OBSERVER'",
            "CENTER": f"'{center}'",
            "START_TIME": f"'{start_time}'",
            "STOP_TIME": f"'{stop_time}'",
            "STEP_SIZE": f"'{step_size}'",
            "QUANTITIES": "'1,9,20,23,24,29'",
            "CSV_FORMAT": "'YES'",
        },
        ttl_seconds=86400,
    )
    result = payload.get("result", "")
    return {
        "target": target,
        "start_time": start_time,
        "stop_time": stop_time,
        "ephemeris": result[-20000:],
        "source": JPL_SOURCE,
        "disclaimer": DISCLAIMER,
    }


def future_signpost_matrix(scenarios: list[dict]) -> dict:
    """Structures plausible future scenarios with signals and trigger actions.

    Each scenario may include name, description, probability, signals, impacts,
    and trigger_actions. Probabilities must be between 0 and 1.
    """
    normalized = []
    probability_total = 0.0
    for scenario in scenarios:
        probability = float(scenario.get("probability", 0))
        if probability < 0 or probability > 1:
            raise ValueError("Scenario probabilities must be between 0 and 1")
        probability_total += probability
        normalized.append(
            {
                "name": scenario.get("name"),
                "description": scenario.get("description"),
                "probability": probability,
                "signals": list(scenario.get("signals", [])),
                "impacts": list(scenario.get("impacts", [])),
                "trigger_actions": list(scenario.get("trigger_actions", [])),
            }
        )
    return {
        "scenarios": normalized,
        "probability_total": probability_total,
        "probabilities_sum_to_one": abs(probability_total - 1.0) < 0.001,
        "guidance": (
            "Use probabilities as planning judgments, not claims of certainty."
        ),
    }


class ForesightAgent(SubAgent):
    name = "foresight"
    description = (
        "Builds structured scenario-planning analyses for business decisions (probable futures, "
        "early-warning signals, trigger actions) and can pull real astronomical position data "
        "from NASA JPL Horizons for astronomy-style requests."
    )
    example = "Map out the possible scenarios for my business over the next 2 years"
    suggestion = "Wanna map out possible future scenarios for your business?"
    requires_context = True

    def __init__(self) -> None:
        self.llm = get_llm(0.1)

    async def run(self, query: str, ctx: AgentContext) -> AgentResult:
        request = query.strip()
        context = ctx.business_context
        transcript = ctx.transcript

        plan = await self._plan(request, context, transcript)
        operation = str(plan.get("operation") or "")
        args = plan.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        raw = await self._dispatch(operation, args)
        summary = await self._summarize(request, context, transcript, raw)

        sources = []
        if raw.get("source"):
            sources = [{"label": "NASA JPL Horizons", "url": str(raw["source"])}]
        return AgentResult(
            text=summary,
            data={"data": raw, "source": raw.get("source", "")},
            message_type="foresight",
            sources=sources,
        )

    async def _plan(self, request: str, context: dict, transcript: str) -> dict:
        """Asks the LLM which foresight operation to run (and builds scenarios
        from free text when the user is doing scenario planning)."""
        chain = FORESIGHT_PLAN_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "user_input": request,
                "context": json.dumps(context, indent=2),
                "transcript": transcript,
            }
        )
        plan = parse_json(response.content)
        if not isinstance(plan, dict) or "operation" not in plan:
            raise ValueError(f"Unexpected foresight plan: {response.content}")
        return plan

    async def _summarize(
        self, request: str, context: dict, transcript: str, raw: dict
    ) -> str:
        """Asks the LLM to turn the raw analysis into a concise business summary."""
        chain = FORESIGHT_SUMMARY_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "request": request,
                "context": json.dumps(context, indent=2),
                "transcript": transcript,
                "data": json.dumps(raw, indent=2),
            }
        )
        return extract_text(response.content).strip()

    async def _dispatch(self, operation: str, args: dict) -> dict:
        if operation == "jpl_horizons_ephemeris":
            return await fetch_jpl_horizons_ephemeris(
                str(args.get("target") or ""),
                str(args.get("start_time") or ""),
                str(args.get("stop_time") or ""),
                str(args.get("step_size") or "1 d"),
                str(args.get("center") or "500@399"),
            )
        if operation == "future_signpost_matrix":
            scenarios = args.get("scenarios")
            if not isinstance(scenarios, list) or not scenarios:
                raise ValueError("Scenario planning needs at least one scenario")
            valid = [s for s in scenarios if isinstance(s, dict)]
            if not valid:
                raise ValueError("Scenario planning needs at least one scenario")
            return future_signpost_matrix(valid)
        raise ValueError(f"Unknown foresight operation: {operation!r}")