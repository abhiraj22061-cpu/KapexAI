import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI, status, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from db_service import connect_db, disconnect_db, db
from redis_service import connect_redis, disconnect_redis, redis

from .models.models import WaitlistSignup, CreateChatSession, UserChatMessage
from .utils.db_utils import get_user, get_session, get_all_sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    await connect_redis()
    yield
    await disconnect_db()
    await disconnect_redis()


app = FastAPI(title="KapexAI Backend", lifespan=lifespan)

# CORS middleware to allow requests from localhost:3000 (frontend dev server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
async def create_chat_session(user_data: CreateChatSession):
    """Creates new chat session and pushes job to redis"""
    user = await get_user(user_data.email)

    if not user:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "user not found with given email"},
        )

    session = await db.session.create(
        data={
            "userId": user.id,
            "business_idea": user_data.content,
        }
    )

    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": user_data.content}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )


@app.post("/push_chat_message")
async def push_chat_message(user_data: UserChatMessage):
    """Pushes chat message to the queue, given the session id"""
    user = await get_user(user_data.email)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "user not found with given email"},
        )

    session = await get_session(user_data.session_id)
    if not session or session.userId != user.id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "session not found for user with given email"},
        )

    job = {"job_id": str(uuid4()), "session_id": session.id, "user_input": user_data.content}
    await redis.lpush("jobs:queue", json.dumps(job))

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "success", "session_id": session.id, "job_id": job["job_id"]},
    )

@app.get("/get_sessions")
async def get_sessions(email: str):
    user = await get_user(email)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"message": "user not found with given email"},
        )

    sessions = await get_all_sessions(user)
    data = [
        {
            "id": s.id,
            "business_idea": s.business_idea,
            "status": str(s.status),
            "created_at": s.created_at.isoformat(),
        }
        for s in sessions
    ]

    return JSONResponse(status_code=status.HTTP_200_OK, content={"data": data})


@app.websocket("/ws/session/{session_id}")
async def websocket_stream(websocket: WebSocket, session_id: str):
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"stream:{session_id}")

    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            await websocket.send_json(data)
            if data.get("type") == "end":
                break

    await pubsub.unsubscribe(f"stream:{session_id}")
    await pubsub.close()
    await websocket.close()
