from prisma import Json

from db_service import db


async def get_user_by_email(email: str):
    return await db.user.find_first(where={"email": email})


async def create_user(email: str, name: str = ""):
    return await db.user.create(data={"email": email, "name": name})


async def update_user_name(user_id: str, name: str):
    return await db.user.update(where={"id": user_id}, data={"name": name})


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


async def add_message(session_id: str, role: str, agent: str, content: dict):
    return await db.message.create(
        data={
            "sessionId": session_id,
            "role": role,
            "agent": agent,
            "content": Json(content),
        }
    )


async def get_resume_state(user_id: str) -> dict:
    session = await get_latest_session(user_id)
    if session is None:
        return {"session_id": None, "answers": {}}

    messages = await db.message.find_many(
        where={"sessionId": session.id},
        order={"created_at": "asc"},
    )

    answers: dict[str, str] = {}
    for msg in messages:
        if (
            msg.agent == "QUESTIONNAIRE"
            and isinstance(msg.content, dict)
            and msg.content.get("type") == "answer"
        ):
            answers[msg.content["key"]] = msg.content["content"]
    return {"session_id": session.id, "answers": answers}
