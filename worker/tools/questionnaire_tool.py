import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from langchain_mistralai import ChatMistralAI

from worker.helpers.json_utils import parse_json
from worker.helpers.messages import last_message, questionnaire_pending
from worker.helpers.persistence import update_session_business_idea
from worker.prompts.questionnaire import (
    MAX_QUESTIONS,
    PARSE_ANSWERS_TEMPLATE,
    PLAN_QUESTIONNAIRE_TEMPLATE,
)
from worker.tools.base import Tool

FACTS_KEYS = ("business_location", "business_vision", "target_customers")


class QuestionnaireTool(Tool):
    name = "questionnaire"
    description = "Gathers the business idea and asks a few targeted questions to build context."
    example = "Start the business questionnaire"
    suggestion = "Wanna fill in the business questionnaire to give me better context?"

    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0)

    async def run(self, state: dict) -> list[dict]:
        if questionnaire_pending(state["messages"]):
            return await self._collect(state)
        return await self._ask(state)

    async def _ask(self, state: dict) -> list[dict]:
        idea = str(state.get("user_input") or "").strip()
        plan = await self._plan(idea)
        facts = plan.get("facts", {}) or {}
        questions = plan.get("questions", [])[:MAX_QUESTIONS]

        answers = {"business_about": idea}
        for key in FACTS_KEYS:
            value = (facts.get(key) or "").strip()
            if value:
                answers[key] = value

        await update_session_business_idea(state["session_id"], idea)

        return [
            {
                "role": "USER",
                "agent": "TOOL",
                "type": "questionnaire_start",
                "content": idea,
            },
            {
                "role": "ASSISTANT",
                "agent": "TOOL",
                "type": "questionnaire",
                "content": "Tell me a bit more so I can help you best. Please answer the following:",
                "questions": questions,
                "facts": answers,
            },
        ]

    async def _collect(self, state: dict) -> list[dict]:
        prior = last_message(state["messages"], "questionnaire")
        questions = prior.get("questions", []) if prior else []
        facts = dict(prior.get("facts", {}) or {}) if prior else {}

        answers_text = str(state.get("user_input") or "").strip()
        parsed = await self._parse(questions, answers_text)
        for question, answer in zip(questions, parsed):
            key = question.get("key", "")
            if key:
                facts[key] = answer

        return [
            {
                "role": "USER",
                "agent": "TOOL",
                "type": "questionnaire_answer",
                "content": answers_text,
                "answers": facts,
            },
            {
                "role": "ASSISTANT",
                "agent": "TOOL",
                "type": "questionnaire_complete",
                "content": "Got it. I now have context about your business.",
                "context": facts,
            },
        ]

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
            raise TypeError(f"Expected a JSON list of answers: {response.content}")
        return [str(a) for a in data]
