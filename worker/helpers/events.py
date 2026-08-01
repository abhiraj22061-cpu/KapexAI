import json

from redis_service import publish

EVENTS_CHANNEL = "kapexai:events"
STREAM_PREFIX = "stream:"


async def publish_event(event_type: str, session_id: str, **data) -> None:
    await publish(
        EVENTS_CHANNEL,
        json.dumps({"type": event_type, "session_id": session_id, **data}),
    )


async def publish_stream(session_id: str, payload: dict) -> None:
    """Publishes a payload to the session's stream channel, which the backend
    WebSocket (`/ws/session/{session_id}`) forwards to the frontend."""
    await publish(f"{STREAM_PREFIX}{session_id}", json.dumps(payload))
