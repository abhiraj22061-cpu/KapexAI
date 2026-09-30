import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from worker.llm import get_llm

from worker.helpers.json_utils import parse_json
from worker.prompts.swot import SWOT_TEMPLATE
from worker.agents.base import AgentContext, AgentResult, SubAgent

_SECTION_LABELS = {
    "strengths": "Strengths",
    "weaknesses": "Weaknesses",
    "opportunities": "Opportunities",
    "threats": "Threats",
}


class SwotAgent(SubAgent):
    name = "swot"
    description = "Creates a SWOT (Strengths, Weaknesses, Opportunities, Threats) analysis for a business."
    example = "Run a SWOT analysis for my business"
    suggestion = "Wanna get a SWOT analysis of your business?"
    requires_context = True

    def __init__(self) -> None:
        self.llm = get_llm(0.4)

    async def run(self, query: str, ctx: AgentContext) -> AgentResult:
        chain = SWOT_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "request": query,
                "context": json.dumps(ctx.business_context, indent=2),
                "transcript": ctx.transcript,
            }
        )
        data = parse_json(response.content)
        if not isinstance(data, dict) or "sections" not in data:
            raise ValueError(f"Unexpected SWOT output: {response.content}")

        return AgentResult(
            text=_format_swot(data),
            data={
                "sections": data.get("sections", {}),
                "summary": data.get("summary", ""),
            },
            message_type="swot",
        )


def _format_swot(data: dict) -> str:
    sections = data.get("sections", {})
    lines = []
    for key, label in _SECTION_LABELS.items():
        lines.append(f"### {label}")
        for item in sections.get(key, []):
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()
