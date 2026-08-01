from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.tools import tool
from langchain_mistralai import ChatMistralAI
from langgraph.prebuilt import create_react_agent

from worker.prompts.research import RESEARCH_PROMPT


@tool
def tavily_search(query: str) -> str:
    """Searches the web with Tavily and returns the top results."""
    results = TavilySearchResults(max_results=3).invoke(query)
    return "\n\n".join(
        f"[{r.get('title', 'No title')}]\n{r.get('content', '')}"
        for r in results
    )


class ResearchAgent:
    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0)
        self.agent = create_react_agent(self.llm, [tavily_search])

    def run(self, answers: dict[str, str]) -> str:
        prompt = RESEARCH_PROMPT.format(
            business_about=answers["business_about"],
            business_location=answers["business_location"],
            business_vision=answers["business_vision"],
        )
        result = self.agent.invoke({"messages": [("human", prompt)]})
        return result["messages"][-1].content
