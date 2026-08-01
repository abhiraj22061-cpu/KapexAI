import json
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from redis_service import push_message

from worker.helpers.events import publish_event
from worker.helpers.persistence import (
    add_message,
    create_session,
    create_user,
    get_resume_state,
    get_user_by_email,
    update_session_business_idea,
    update_user_name,
)
from worker.prompts.questionnaire import QUESTIONS

NAME_PROMPT = "What is your name?"
USER_QUEUE = "user_messages"


class QuestionnaireAgent:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.user_id: str | None = None
        self.email: str = ""
        self.answers: dict[str, str] = {}

    async def ask(self) -> dict[str, str]:
        email = self._prompt("What is your email?")
        self.email = email

        user = await get_user_by_email(email)
        if user is None:
            user = await create_user(email)
            self.user_id = user.id
        else:
            self.user_id = user.id
            if user.name:
                self.answers["user_name"] = user.name

        resume = await get_resume_state(self.user_id)
        self.session_id = resume["session_id"]
        for key, value in resume["answers"].items():
            self.answers.setdefault(key, value)

        if self.session_id is None:
            session = await create_session(self.user_id)
            self.session_id = session.id

        await publish_event("session_started", self.session_id, email=self.email)

        prompts = [("user_name", NAME_PROMPT)] + QUESTIONS
        for key, question in prompts:
            if key in self.answers:
                print(f"\nQ: {question}\nA: {self.answers[key]} (from previous session)")
                continue

            print(f"\nQ: {question}")
            answer = self._prompt("A")
            self.answers[key] = answer

            await add_message(
                self.session_id,
                role="USER",
                agent="QUESTIONNAIRE",
                content={"type": "answer", "key": key, "content": answer},
            )
            if key == "user_name":
                await update_user_name(self.user_id, answer)

            await push_message(
                USER_QUEUE,
                json.dumps(
                    {
                        "session_id": self.session_id,
                        "key": key,
                        "content": answer,
                        "agent": "QUESTIONNAIRE",
                    }
                ),
            )
            await publish_event("answer_received", self.session_id, key=key, content=answer)

        await publish_event("questionnaire_complete", self.session_id, answers=self.answers)

        if "business_about" in self.answers:
            await update_session_business_idea(self.session_id, self.answers["business_about"])

        return self.answers

    @staticmethod
    def _prompt(label: str) -> str:
        answer = input(f"{label}: ").strip()
        if answer.lower() in ("quit", "exit", "q"):
            raise SystemExit("Exited.")
        while not answer:
            print("Please provide an answer.")
            answer = input(f"{label}: ").strip()
        return answer
