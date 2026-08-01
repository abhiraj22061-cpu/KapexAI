import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from langchain_mistralai import ChatMistralAI

from worker.helpers.events import publish_stream
from worker.helpers.persistence import add_message, update_session_business_idea
from worker.prompts.questionnaire import (
    MAX_QUESTIONS,
    PARSE_ANSWERS_TEMPLATE,
    PLAN_QUESTIONNAIRE_TEMPLATE,
)

FACTS_KEYS = ("business_location", "business_vision", "target_customers")


def _as_text(value) -> str:
    if isinstance(value, (tuple, list)):
        value = value[-1] if value else ""
    return str(value or "").strip()


class QuestionnaireAgent:
    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0)

    async def start(self, state: dict) -> dict:
        """Consumes the initial business idea, extracts known facts and asks the
        user up to MAX_QUESTIONS deep questions all at once."""
        session_id = state["session_id"]
        idea = _as_text(state["user_input"])

        plan = await self._plan(idea)
        facts = plan.get("facts", {}) or {}
        questions = plan.get("questions", [])[:MAX_QUESTIONS]

        answers = {"business_about": idea}
        for key in FACTS_KEYS:
            value = (facts.get(key) or "").strip()
            if value:
                answers[key] = value

        await add_message(
            session_id,
            "USER",
            "QUESTIONNAIRE",
            {
                "type": "idea",
                "content": idea,
                "facts": {k: answers[k] for k in FACTS_KEYS if k in answers},
            },
        )
        await update_session_business_idea(session_id, idea)

        await publish_stream(
            session_id,
            {
                "type": "questionnaire",
                "content": "Tell me a bit more so I can build the best report. Please answer the following:",
                "questions": questions,
            },
        )

        return {"answers": answers, "questions": questions, "phase": "QUESTIONNAIRE"}

    async def collect_answers(self, state: dict) -> dict:
        """Consumes the user's answers to all questions in one message."""
        session_id = state["session_id"]
        questions = state.get("questions", [])
        answers_text = _as_text(state["user_input"])

        parsed_answers = await self._parse(questions, answers_text)
        answers = dict(state.get("answers", {}))
        for question, answer in zip(questions, parsed_answers):
            key = question.get("key", "")
            if key:
                answers[key] = answer

        await add_message(
            session_id,
            "USER",
            "QUESTIONNAIRE",
            {"type": "answers", "answers": answers, "content": answers_text},
        )
        await publish_stream(
            session_id,
            {
                "type": "questionnaire_complete",
                "content": "Got it. Running research now, this can take a minute...",
            },
        )

        return {"answers": answers, "phase": "RESEARCH"}

    async def _plan(self, idea: str) -> dict:
        chain = PLAN_QUESTIONNAIRE_TEMPLATE | self.llm
        response = await chain.ainvoke({"idea": idea, "max_questions": MAX_QUESTIONS})
        plan = parse_json(response.content)
        if not isinstance(plan, dict) or "questions" not in plan:
            raise ValueError(f"Unexpected questionnaire plan: {response.content}")
        return plan

    async def _parse(self, questions: list[dict], answers_text: str) -> list[str]:
        chain = PARSE_ANSWERS_TEMPLATE | self.llm
        response = await chain.ainvoke(
            {"questions": json.dumps(questions), "answers": answers_text}
        )
        data = parse_json(response.content)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON list of answers: {response.content}")
        return [str(a) for a in data]


def parse_json(raw: str):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)
