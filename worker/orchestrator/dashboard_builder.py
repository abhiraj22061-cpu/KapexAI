"""Turns subagent results into a dashboard document.

Two steps: a deterministic pass maps each subagent result into generic
sections (so the dashboard always has content even if the LLM is down), then
one narrative pass gives it a name, headline and grounded suggestions.
Suggestion provenance is enforced in Python — the LLM can only reference the
sources the search actually returned, by index; anything else is labelled
"ai" instead of being dressed up as a citation.
"""

import json
import logging

from worker.agents.base import AgentResult
from worker.helpers.json_utils import parse_json
from worker.llm import get_llm
from worker.prompts.orchestrator import DASHBOARD_NARRATIVE_TEMPLATE

logger = logging.getLogger(__name__)

DASHBOARD_LABELS = {
    "swot": "SWOT Analysis",
    "market_analysis": "Market Analysis",
    "competitor_analysis": "Competitor Analysis",
    "financial_analysis": "Financial Analysis",
    "risk_analysis": "Risk Analysis",
    "scenario_analysis": "Scenario Analysis",
    "general": "Business Analysis",
}

_SWOT_QUADRANTS = (
    ("strengths", "Strengths"),
    ("weaknesses", "Weaknesses"),
    ("opportunities", "Opportunities"),
    ("threats", "Threats"),
)

# Friendly heading per inline card type when a result becomes a section.
_SECTION_HEADINGS = {
    "research": "Market Research",
    "economics": "Economic Data",
    "foresight": "Scenario Outlook",
    "legal_research": "Legal & Regulatory Findings",
    "case_search": "Case Law References",
    "issue_register": "Compliance Issues",
    "swot": "SWOT",
    "astrology": "Astrology Perspective",
    "finance": "Finance Analysis",
    "indian_finance": "Finance Calculation",
}

_MAX_SECTIONS = 8
_MAX_TEXT = 6000
_MAX_ITEMS = 12
_MAX_SOURCES = 12


class DashboardBuilder:
    def __init__(self) -> None:
        self.llm = get_llm(0.3)

    async def build(
        self,
        *,
        kind: str,
        request: str,
        context: dict,
        results: list[tuple[str, AgentResult]],
    ) -> dict:
        label = DASHBOARD_LABELS.get(kind, DASHBOARD_LABELS["general"])
        sections = _sections(results)
        sources = _sources(results)

        try:
            narrative = await self._narrative(label, request, context, sections, sources)
        except Exception:
            logger.exception("Dashboard narrative pass failed; using fallback")
            narrative = {}

        return {
            "kind": kind,
            "kind_label": label,
            "name": (_first(narrative.get("name")) or label)[:80],
            "title": _first(narrative.get("title")) or label,
            "subtitle": _first(narrative.get("subtitle")),
            "intro": _first(narrative.get("intro")) or f"Your {label} dashboard is ready.",
            "summary": _first(narrative.get("summary")),
            "sections": sections,
            "suggestions": _suggestions(narrative.get("suggestions"), sources),
            "sources": sources,
        }

    async def _narrative(
        self,
        label: str,
        request: str,
        context: dict,
        sections: list[dict],
        sources: list[dict],
    ) -> dict:
        chain = DASHBOARD_NARRATIVE_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "kind_label": label,
                "request": request,
                "context": json.dumps(context, indent=2),
                "sections": json.dumps(sections, indent=2),
                "sources": json.dumps(sources, indent=2),
            }
        )
        data = parse_json(response.content)
        return data if isinstance(data, dict) else {}


# ── deterministic draft ─────────────────────────────────────────


def _sections(results: list[tuple[str, AgentResult]]) -> list[dict]:
    out: list[dict] = []
    for agent_name, result in results:
        if len(out) >= _MAX_SECTIONS:
            break
        out.extend(_sections_for(agent_name, result))
    return out[:_MAX_SECTIONS]


def _sections_for(agent_name: str, result: AgentResult) -> list[dict]:
    data = result.data or {}
    heading = _SECTION_HEADINGS.get(result.message_type or "", agent_name)

    if result.message_type == "swot" and isinstance(data.get("sections"), dict):
        quadrants = []
        for key, label in _SWOT_QUADRANTS:
            items = [str(i) for i in data["sections"].get(key) or []][:_MAX_ITEMS]
            if items:
                quadrants.append({"heading": label, "kind": "bullets", "items": items})
        return quadrants

    if isinstance(data.get("insights"), list) and data["insights"]:
        items = [str(i) for i in data["insights"] if str(i).strip()][:_MAX_ITEMS]
        if items:
            return [{"heading": heading, "kind": "bullets", "items": items}]

    text = (result.text or "").strip()[:_MAX_TEXT]
    if not text:
        return []
    return [{"heading": heading, "kind": "text", "text": text}]


def _sources(results: list[tuple[str, AgentResult]]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for _, result in results:
        for source in result.sources or []:
            url = str(source.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"label": _first(source.get("label")) or url, "url": url})
            if len(out) >= _MAX_SOURCES:
                return out
    return out


def _suggestions(raw: object, sources: list[dict]) -> list[dict]:
    """Validates the LLM's suggestions: a source_index may only point at a
    real source; anything else (bad index, null, prose URL) becomes "ai"."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        text = _first(item.get("text"))
        if not text:
            continue
        idx = item.get("source_index")
        source = None
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(sources):
            source = sources[idx]
        out.append(
            {"text": text, "source": source, "origin": "sourced" if source else "ai"}
        )
    return out


def _first(value: object) -> str:
    return str(value).strip() if value is not None else ""
