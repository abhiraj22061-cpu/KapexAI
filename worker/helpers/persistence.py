from prisma import Json

from db_service import db


async def get_user_by_email(email: str):
    return await db.user.find_first(where={"email": email})


async def create_user(email: str, name: str = ""):
    return await db.user.create(data={"email": email, "name": name})


async def update_user_name(user_id: str, name: str):
    return await db.user.update(where={"id": user_id}, data={"name": name})


async def get_session(session_id: str):
    return await db.session.find_unique(where={"id": session_id})


async def get_latest_session(user_id: str):
    return await db.session.find_first(
        where={"userId": user_id},
        order={"created_at": "desc"},
    )


async def create_session(user_id: str, business_idea: str = ""):
    return await db.session.create(
        data={"userId": user_id, "business_idea": business_idea}
    )


async def update_session_business_idea(session_id: str, business_idea: str):
    return await db.session.update(
        where={"id": session_id}, data={"business_idea": business_idea}
    )


async def mark_session_failed(session_id: str):
    return await db.session.update(
        where={"id": session_id}, data={"status": "FAILED"}
    )


async def add_message(session_id: str, role: str, agent: str, content: dict):
    return await db.message.create(
        data={
            "sessionId": session_id,
            "role": role,
            "agent": agent,
            "content": Json(content),
        }
    )


def _empty_state(session_id: str, user_id: str) -> dict:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "user_input": "",
        "answers": {},
        "questions": [],
        "research_result": "",
        "report": "",
        "guardrail": {},
        "phase": "QUESTIONNAIRE",
    }


async def build_state_from_db(session) -> dict:
    """Rebuilds the langgraph state for a session from its chat history so the
    workflow knows where to resume from. Order-insensitive: relies on message
    content types, not created_at ordering (which can tie within a microsecond)."""
    messages = await db.message.find_many(where={"sessionId": session.id})

    state = _empty_state(session.id, session.userId)
    has_answers = False
    has_report = False

    for msg in messages:
        content = msg.content
        if not isinstance(content, dict):
            continue
        if content.get("type") == "idea":
            state["answers"]["business_about"] = content.get("content", "")
            state["answers"].update(content.get("facts", {}))
        elif content.get("type") == "answers":
            state["answers"].update(content.get("answers", {}))
            has_answers = True
        elif content.get("type") == "report":
            state["report"] = content.get("content", "")
            has_report = True

    if has_report:
        state["phase"] = "COMPLETE"
    elif has_answers:
        state["phase"] = "RESEARCH"

    if "business_about" not in state["answers"] and session.business_idea:
        state["answers"]["business_about"] = session.business_idea

    return state
