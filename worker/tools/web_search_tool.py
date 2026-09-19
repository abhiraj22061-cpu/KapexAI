import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from worker.helpers.messages import business_context, format_transcript
from worker.prompts.research_websearch import RESEARCH_WEBSEARCH_PROMPT
from worker.tools.base import Tool
from worker.tools.tavily_search import tavily_search


class WebSearchTool(Tool):
    name = "web_search"
    description = "Perform live web research on a topic, competitor, market, or any question using internet search."
    example = "Search for my top competitors.."
    suggestion = "Wanna do a web search on your top competitors?"
    requires_context = True

    def __init__(self) -> None:
        self.llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash", temperature=0.2)
        # The system prompt is built per request (with the business context and
        # message history), so the agent is created without a static prompt.
        self.agent = create_react_agent(self.llm, [tavily_search])

    def run(self, state: dict) -> list[dict]:
        prompt = str(state.get("user_input") or "")
        context = business_context(state["messages"])
        transcript = format_transcript(state["messages"])

        system = RESEARCH_WEBSEARCH_PROMPT.format(
            context=json.dumps(context, indent=2),
            transcript=transcript,
        )
        result = self.agent.invoke(
            {
                "messages": [
                    SystemMessage(content=system),
                    HumanMessage(content=prompt),
                ]
            }
        )
        content = result["messages"][-1].content
        return [
            {
                "role": "USER",
                "agent": "TOOL",
                "type": "research_request",
                "content": prompt,
            },
            {
                "role": "ASSISTANT",
                "agent": "TOOL",
                "type": "research",
                "content": content,
            },
        ]
