from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from langchain_mistralai import ChatMistralAI
from langgraph.prebuilt import create_react_agent

from worker.prompts.research import RESEARCH_PROMPT
from worker.tools import tavily_search


class ResearchAgent:
    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0)
        self.agent = create_react_agent(self.llm, [tavily_search])

    def run(self, answers: dict[str, str]) -> str:
        prompt = RESEARCH_PROMPT.format(
            business_about=answers.get("business_about", ""),
            business_location=answers.get("business_location", ""),
            business_vision=answers.get("business_vision", ""),
        )
        result = self.agent.invoke({"messages": [("human", prompt)]})
        return result["messages"][-1].content
