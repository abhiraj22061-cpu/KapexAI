import json
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from langchain_core.messages import HumanMessage, SystemMessage
from worker.llm import get_llm
from langgraph.prebuilt import create_react_agent

from worker.agents.base import AgentContext, AgentResult, SubAgent
from worker.prompts.research_websearch import RESEARCH_WEBSEARCH_PROMPT
from worker.tools.tavily_search import tavily_search


class WebSearchAgent(SubAgent):
    name = "web_search"
    description = "Perform live web research on a topic, competitor, market, or any question using internet search."
    example = "Search for my top competitors.."
    suggestion = "Wanna do a web search on your top competitors?"
    requires_context = True

    def __init__(self) -> None:
        self.llm = get_llm(0.2)
        # The system prompt is built per request (with the business context and
        # message history), so the agent is created without a static prompt.
        self.agent = create_react_agent(self.llm, [tavily_search])

    def run(self, query: str, ctx: AgentContext) -> AgentResult:
        system = RESEARCH_WEBSEARCH_PROMPT.format(
            context=json.dumps(ctx.business_context, indent=2),
            transcript=ctx.transcript,
        )
        result = self.agent.invoke(
            {
                "messages": [
                    SystemMessage(content=system),
                    HumanMessage(content=query),
                ]
            }
        )
        content = result["messages"][-1].content
        if not isinstance(content, str):
            content = str(content)
        return AgentResult(
            text=content,
            message_type="research",
            sources=_markdown_sources(content),
        )


def _markdown_sources(text: str) -> list[dict]:
    """Extracts `[label](http...)` links from the research answer so dashboards
    can attribute suggestions to the pages the search actually surfaced."""
    sources: list[dict] = []
    seen: set[str] = set()
    for label, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
        if url in seen:
            continue
        seen.add(url)
        sources.append({"label": label.strip() or url, "url": url})
    return sources[:10]
