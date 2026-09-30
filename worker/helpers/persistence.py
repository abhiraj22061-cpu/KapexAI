from db_service import db
from prisma import Json


async def get_user_by_email(email: str):
    return await db.user.find_first(where={"email": email})


async def create_user(email: str, name: str = ""):
    return await db.user.create(data={"email": email, "name": name})


async def update_user_name(user_id: str, name: str):
    return await db.user.update(where={"id": user_id}, data={"name": name})


async def get_session(session_id: str):
    return await db.session.find_unique(where={"id": session_id})


async def get_business_profile(user_id: str):
    """Returns the user's business profile row, or None when the user has no
    profile (accounts created before the feature shipped)."""
    return await db.businessprofile.find_unique(where={"userId": user_id})


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

async def mark_session_active(session_id: str):
    return await db.session.update(where={"id": session_id}, data={"status": "ACTIVE"})


async def add_message(session_id: str, role: str, agent: str, content: dict):
    return await db.message.create(
        data={
            "sessionId": session_id,
            "role": role,
            "agent": agent,
            "content": Json(content),
        }
    )


async def create_dashboard(session_id: str, name: str, data: dict):
    return await db.dashboard.create(
        data={
            "sessionId": session_id,
            "name": name,
            "data": Json(data),
        }
    )


async def expand_dashboards(messages: list[dict]) -> list[dict]:
    """Re-attaches `dashboard_data` to dashboard entries by batch-loading the
    referenced rows. The message log only stores the reference, so a dashboard
    edit (or a cache rebuilt before the feature existed) always resolves to the
    current document. Entries whose row is missing stay as bare references."""
    ids = [
        m["dashboard_id"]
        for m in messages
        if m.get("type") == "dashboard"
        and m.get("dashboard_id")
        and "dashboard_data" not in m
    ]
    if not ids:
        return messages
    rows = await db.dashboard.find_many(where={"id": {"in": ids}})
    by_id = {row.id: row.data for row in rows}
    out = []
    for msg in messages:
        data = by_id.get(msg.get("dashboard_id"))
        if data is not None and "dashboard_data" not in msg:
            msg = {**msg, "dashboard_data": data}
        out.append(msg)
    return out


def _empty_state(session_id: str, user_id: str) -> dict:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "user_input": "",
        "messages": [],
    }


async def build_state_from_db(session) -> dict:
    """Rebuilds the message-log state for a session from its chat history,
    ordered by creation time so the conversation is reconstructed in sequence."""
    messages = await db.message.find_many(
        where={"sessionId": session.id},
        order={"created_at": "asc"},
    )

    state = _empty_state(session.id, session.userId)
    log = []
    for msg in messages:
        content = msg.content
        if not isinstance(content, dict):
            content = {}
        log.append({"role": msg.role, "agent": msg.agent, **content})
    state["messages"] = log
    return state
