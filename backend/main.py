import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI, status, WebSocket, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from prisma import Json

from db_service import connect_db, disconnect_db, db
from redis_service import connect_redis, disconnect_redis, redis

from .models.models import (
    WaitlistSignup,
    CreateChatSession,
    UserChatMessage,
    SubmitQuestionnaireAnswersRequest,
    SubmitQuestionnaireClarificationRequest,
    RenameSessionRequest,
    DeleteSessionRequest,
    BusinessProfileRequest,
)
from .utils.db_utils import (
    business_profile_is_empty,
    ensure_business_profile,
    get_session,
    get_all_sessions,
)
from .routers import auth
from .middleware.auth import get_current_user

# Marks a session's most recent message that is still being processed by the
# worker. Used so other tabs can show the in-flight message + typing indicator
# and pick up the live stream. Cleared by the worker when the job completes.
PENDING_KEY = "pending:{session_id}"
PENDING_TTL = 5 * 60  # seconds

# Per-turn buffer of streamed frames (Redis list, written by the worker).
# The WebSocket endpoint tails it instead of relying on pub/sub alone, so a
# client that connects after a fast turn finished still gets the frames.
# NOTE: mirrored in worker/helpers/events.py (backend must not import worker).
STREAM_FRAMES_KEY = "stream:{session_id}:frames"
FRAMES_TTL = 5 * 60  # seconds


async def mark_pending(session_id: str, content: str, msg_type: str) -> None:
    """Records the user's latest in-flight message and flags the session as
    PENDING so any tab can surface it while the worker is still replying."""
    await redis.set(
        PENDING_KEY.format(session_id=session_id),
        json.dumps({"content": content, "type": msg_type}),
        ex=PENDING_TTL,
    )
    # A new turn owns a fresh frame buffer — drop the previous turn's frames
    # so a late subscriber can never replay the wrong reply.
    await redis.delete(STREAM_FRAMES_KEY.format(session_id=session_id))
    await db.session.update(where={"id": session_id}, data={"status": "PENDING"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    await connect_redis()
    yield
    await disconnect_db()
    await disconnect_redis()


app = FastAPI(title="KapexAI Backend", lifespan=lifespan)

# CORS middleware - reads from env var for production flexibility
import os
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/waitlist")
async def join_waitlist(signup: WaitlistSignup):
    """Add email (and optional name) to waitlist. Returns success message."""
    # In a real app, you'd save to database here, e.g.:
    # await db.waitlist.create(data={"email": signup.email, "name": signup.name})
    return {"message": "Successfully joined the waitlist!", "email": signup.email}


@app.post("/create_chat_session")
async def create_chat_session(user_data: CreateChatSession, current_user = Depends(get_current_user)):
    """Creates new chat session and pushes job to redis"""
    session = await db.session.create(
        data={
            "userId": current_user.id,
            "business_idea": user_data.content,
        }
    )

    # Pending is marked before the job is queued so the worker can never
    # finish (and clear it) before it was set — that ordering bug left
    # sessions stuck "PENDING" with a typing indicator that never went away.
    await mark_pending(session.id, user_data.content, "chat")
    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": user_data.content}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )


@app.post("/push_chat_message")
async def push_chat_message(user_data: UserChatMessage, current_user = Depends(get_current_user)):
    """Pushes chat message to the queue, given the session id"""
    session = await get_session(user_data.session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    await mark_pending(session.id, user_data.content, "chat")
    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": user_data.content}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )


@app.post("/submit_questionnaire_answers")
async def submit_questionnaire_answers(user_data: SubmitQuestionnaireAnswersRequest, current_user = Depends(get_current_user)):
    """Submits structured questionnaire answers for a session. The answers are
    pushed as a job whose `user_input` carries a structured payload, so the
    worker can fold them into the business context without re-parsing free text."""
    session = await get_session(user_data.session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    payload = json.dumps(
        {
            "kind": "questionnaire_answers",
            "answers": [{"key": a.key, "answer": a.answer} for a in user_data.answers],
        }
    )
    content = "\n".join(
        f"{i + 1}) {a.answer or 'Skipped'}" for i, a in enumerate(user_data.answers)
    )
    await mark_pending(session.id, content, "questionnaire_answer")
    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": payload}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )

@app.post("/submit_questionnaire_clarification")
async def submit_questionnaire_clarification(user_data: SubmitQuestionnaireClarificationRequest, current_user = Depends(get_current_user)):
    """Requests a plain-language explanation of specific questionnaire questions.
    Pushed as a structured job so the worker can explain without re-parsing free
    text or rejecting the request as a bad answer."""
    session = await get_session(user_data.session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    payload = json.dumps(
        {"kind": "questionnaire_clarification", "keys": user_data.keys}
    )
    await mark_pending(session.id, "Asked for a simpler explanation", "chat")
    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": payload}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )

@app.get("/get_sessions")
async def get_sessions(current_user = Depends(get_current_user)):
    sessions = await get_all_sessions(current_user)

    # Dashboards are grouped into their sessions in one query so the sidebar
    # can render the per-session dashboard lists without N+1 calls.
    session_ids = [s.id for s in sessions]
    dashboards_by_session: dict[str, list[dict]] = {sid: [] for sid in session_ids}
    if session_ids:
        rows = await db.dashboard.find_many(
            where={"sessionId": {"in": session_ids}},
            order={"created_at": "asc"},
        )
        for d in rows:
            dashboards_by_session.setdefault(d.sessionId, []).append(
                {"id": d.id, "name": d.name, "created_at": d.created_at.isoformat()}
            )

    data = [
        {
            "id": s.id,
            "business_idea": s.business_idea,
            "status": str(s.status),
            "created_at": s.created_at.isoformat(),
            "dashboards": dashboards_by_session.get(s.id, []),
        }
        for s in sessions
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": data})


@app.get("/get_messages")
async def get_messages(session_id: str, current_user = Depends(get_current_user)):
    """Returns the message log for a session, ordered oldest → newest."""
    session = await get_session(session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    messages = await db.message.find_many(
        where={"sessionId": session_id},
        order={"created_at": "asc"},
    )

    data = []
    for m in messages:
        content = m.content
        if not isinstance(content, dict):
            content = {}
        data.append(
            {
                "id": m.id,
                "role": str(m.role),
                "agent": str(m.agent),
                "created_at": m.created_at.isoformat(),
                **content,
            }
        )

    pending_raw = await redis.get(PENDING_KEY.format(session_id=session_id))
    pending = json.loads(pending_raw) if pending_raw else None

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"data": data, "pending": pending}
    )


@app.get("/get_dashboards")
async def get_dashboards(session_id: str, current_user = Depends(get_current_user)):
    """Lists every dashboard generated in a session (id + name + timestamp),
    oldest first — drives the dashboard tab bar and the sidebar section."""
    session = await get_session(session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    rows = await db.dashboard.find_many(
        where={"sessionId": session_id},
        order={"created_at": "asc"},
    )
    data = [
        {"id": d.id, "name": d.name, "created_at": d.created_at.isoformat()}
        for d in rows
    ]
    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": data})


@app.get("/get_dashboard")
async def get_dashboard(dashboard_id: str, current_user = Depends(get_current_user)):
    """Returns one dashboard with its full JSON payload (read-only; dashboards
    are created by the worker and never edited by the user)."""
    dashboard = await db.dashboard.find_unique(where={"id": dashboard_id})
    if not dashboard:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "dashboard not found"},
        )

    session = await get_session(dashboard.sessionId)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "dashboard not found for user"},
        )

    data = dashboard.data if isinstance(dashboard.data, dict) else {}
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "data": {
                "id": dashboard.id,
                "name": dashboard.name,
                "session_id": dashboard.sessionId,
                "created_at": dashboard.created_at.isoformat(),
                "data": data,
            }
        },
    )


@app.post("/rename_session")
async def rename_session(user_data: RenameSessionRequest, current_user = Depends(get_current_user)):
    """Renames a chat session (its `business_idea` title) for the current user."""
    session = await get_session(user_data.session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    name = user_data.name.strip()
    if not name:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": "name cannot be empty"},
        )

    updated = await db.session.update(
        where={"id": session.id},
        data={"business_idea": name},
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": "success",
            "session_id": session.id,
            "business_idea": updated.business_idea,
        },
    )


@app.post("/delete_session")
async def delete_session(user_data: DeleteSessionRequest, current_user = Depends(get_current_user)):
    """Deletes a chat session along with all of its messages and dashboards."""
    session = await get_session(user_data.session_id)
    if not session or session.userId != current_user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user"},
        )

    # Drop any cached graph state so it can't be resurrected by the worker.
    await redis.delete(f"langgraph_state:{session.id}")
    await redis.delete(PENDING_KEY.format(session_id=session.id))
    await redis.delete(STREAM_FRAMES_KEY.format(session_id=session.id))

    await db.message.delete_many(where={"sessionId": session.id})
    await db.dashboard.delete_many(where={"sessionId": session.id})
    await db.session.delete(where={"id": session.id})

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "success", "session_id": session.id},
    )


@app.get("/get_business_profile")
async def get_business_profile(current_user = Depends(get_current_user)):
    """Returns the current user's business profile content (an object with the
    standard profile keys; empty values when nothing has been filled in yet)."""
    row = await ensure_business_profile(current_user.id)
    content = row.content
    if not isinstance(content, dict):
        content = {}
    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": content})


@app.post("/update_business_profile")
async def update_business_profile(profile_data: BusinessProfileRequest, current_user = Depends(get_current_user)):
    """Upserts the current user's business profile. Each field is optional; empty
    strings are stored as-is so a user can also clear a field."""
    await ensure_business_profile(current_user.id)
    content = {k: v for k, v in profile_data.model_dump().items() if v is not None}
    updated = await db.businessprofile.update(
        where={"userId": current_user.id},
        data={"content": Json(content)},
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": "success", "data": updated.content},
    )


@app.websocket("/ws/session/{session_id}")
async def websocket_stream(websocket: WebSocket, session_id: str):
    """Tails a session's frame buffer to the client.

    The Redis list is the source of truth (the worker appends to it and the
    pub/sub publish is just a wakeup), so a client that connects *after* a
    fast turn already finished — or between two frames during a reconnect —
    still replays everything it missed instead of losing the reply."""
    await websocket.accept()

    frames_key = STREAM_FRAMES_KEY.format(session_id=session_id)
    pending_key = PENDING_KEY.format(session_id=session_id)
    channel = f"stream:{session_id}"

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    cursor = 0
    try:
        while True:
            raw_frames = await redis.lrange(frames_key, cursor, -1)
            if raw_frames:
                cursor += len(raw_frames)
                for raw in raw_frames:
                    data = json.loads(raw)
                    if not await _safe_send(websocket, data):
                        return  # client disconnected mid-stream
                    if data.get("type") == "end":
                        return  # turn fully delivered
                continue

            if not await redis.get(pending_key):
                # Buffer drained and no job in flight: the turn is over
                # (or nothing ever streamed — same signal as before).
                await _safe_send(websocket, {"type": "end"})
                return

            # Job still running — wait for the worker's wakeup publish
            # (the 1s timeout also acts as a poll for missed wakeups).
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _safe_send(websocket: WebSocket, data: dict) -> bool:
    """Sends a frame, returning False when the client has already gone. A tab
    that closed mid-stream (e.g. navigating away) must not crash the endpoint."""
    try:
        await websocket.send_json(data)
        return True
    except (WebSocketDisconnect, RuntimeError):
        return False
