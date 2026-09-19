import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from langchain_google_genai import ChatGoogleGenerativeAI

from worker.prompts.chat import CHAT_TEMPLATE


class ChatAgent:
    def __init__(self) -> None:
        self.llm = ChatGoogleGenerativeAI(model="gemini-3.6-flash", temperature=0.5)

    async def run(
        self,
        user_input: str,
        transcript: str,
        context: dict,
        tools: list[dict],
    ) -> str:
        chain = CHAT_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "user_input": user_input,
                "transcript": transcript,
                "context": json.dumps(context, indent=2),
                "tools": json.dumps([t["name"] for t in tools], indent=2),
            }
        )
        return response.content
