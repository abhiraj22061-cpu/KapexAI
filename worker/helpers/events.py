import json

from redis_service import publish

EVENTS_CHANNEL = "kapexai:events"


async def publish_event(event_type: str, session_id: str, **data) -> None:
    await publish(
        EVENTS_CHANNEL,
        json.dumps({"type": event_type, "session_id": session_id, **data}),
    )
