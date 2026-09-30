"""State load/save and the job lifecycle around the orchestrator.

The orchestrator owns the turn itself (plan → run subagents → compose →
stream); this module only prepares state, records the outcome and handles
failures — exactly the contract `worker/main.py` relies on.
"""

import json
import logging
from typing import TypedDict

import httpx
from redis_service import redis

from worker.helpers.events import publish_stream
from worker.helpers.messages import inject_business_profile
from worker.helpers.persistence import (
    build_state_from_db,
    expand_dashboards,
    get_business_profile,
    get_session,
    mark_session_active,
    mark_session_failed,
)
from worker.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

STATE_KEY = "langgraph_state:{session_id}"
STATE_TTL = 60 * 60 * 24  # 24 hours
PENDING_KEY = "pending:{session_id}"


class State(TypedDict):
    session_id: str
    user_id: str
    user_input: str
    messages: list[dict]


async def load_state(session_id: str) -> State:
    raw = await redis.get(STATE_KEY.format(session_id=session_id))
    if raw:
        state = json.loads(raw)
        state.setdefault("messages", [])
        state.setdefault("user_id", "")
        # Legacy keys from the router era — drop them so they never come back.
        state.pop("intent", None)
        state.pop("tool", None)
    else:
        session = await get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        state = await build_state_from_db(session)

    # Inject the user's business profile into the message log. This is done fresh
    # on every load (replacing any stale cached entry), so a profile edit shows
    # up in the very next job — the messages/cache never go stale.
    row = await get_business_profile(state.get("user_id", ""))
    profile = row.content if row else {}
    if isinstance(profile, dict):
        state["messages"] = inject_business_profile(state["messages"], profile)

    # Resolve bare dashboard references into their documents so agents and the
    # transcript can read them without another query.
    state["messages"] = await expand_dashboards(state["messages"])
    return state


async def save_state(session_id: str, state: State) -> None:
    await redis.set(
        STATE_KEY.format(session_id=session_id),
        json.dumps(state),
        ex=STATE_TTL,
    )


async def process_job(job: dict, orchestrator: Orchestrator) -> State:
    session_id = job["session_id"]
    job_id = str(job.get("job_id", "") or "")
    user_input = str(job.get("user_input", "") or "")

    try:
        state = await load_state(session_id)
        state["user_input"] = user_input
        result = await orchestrator.handle(state)
        await save_state(session_id, result)
        # The job is done — the backend's in-flight marker is no longer needed.
        await redis.delete(PENDING_KEY.format(session_id=session_id))
        await mark_session_active(session_id)
        return result
    except Exception as exc:
        logger.exception("Failed to process session %s (job %s)", session_id, job_id)

        rate_limited = False
        err = exc
        while err is not None:
            if isinstance(err, httpx.HTTPStatusError) and err.response.status_code == 429:
                rate_limited = True
                break
            err = err.__cause__ or err.__context__

        error_content = (
            "Oops, looks like Kapex has hit its API limit. Please try again in a minute."
            if rate_limited
            else (f"Job {job_id} failed" if job_id else "Job failed")
        )

        try:
            await mark_session_failed(session_id)
            await redis.delete(PENDING_KEY.format(session_id=session_id))
            await publish_stream(
                session_id,
                {
                    "type": "error",
                    "job_id": job_id,
                    "content": error_content,
                },
            )
        except Exception:
            logger.exception("Failed to notify error for session %s", session_id)
        raise
