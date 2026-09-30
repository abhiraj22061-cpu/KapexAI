import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from worker.llm import get_llm

from worker.helpers.json_utils import extract_text
from worker.prompts.chat import CHAT_TEMPLATE


class ChatAgent:
    def __init__(self) -> None:
        self.llm = get_llm(0.5)

    async def run(
        self,
        user_input: str,
        transcript: str,
        context: dict,
        tools: list[dict],
        notes: str = "",
    ) -> str:
        chain = CHAT_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {
                "user_input": user_input,
                "transcript": transcript,
                "context": json.dumps(context, indent=2),
                "tools": json.dumps([t["name"] for t in tools], indent=2),
                "notes": notes or "(none)",
            }
        )
        return extract_text(response.content)
