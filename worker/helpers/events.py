import json

from redis_service import publish, redis

STREAM_PREFIX = "stream:"
# Buffered copy of each turn's frames (a Redis list). The pub/sub publish is
# only a wakeup — the list is what a late-connecting WebSocket replays, so
# frames published before any socket subscribed are never lost.
# NOTE: mirrored in backend/main.py (backend must not import worker code).
FRAMES_KEY = STREAM_PREFIX + "{session_id}:frames"
FRAMES_TTL = 5 * 60  # seconds; long enough for a reconnecting client


async def publish_stream(session_id: str, payload: dict) -> None:
    """Buffers a frame for replay and wakes up live WebSocket subscribers.

    The backend's WebSocket endpoint tails the `stream:{session_id}:frames`
    list as its source of truth and uses the pub/sub message only to know
    when to read again — this is what makes a client that connects after a
    fast turn finished still see the whole reply."""
    raw = json.dumps(payload)
    key = FRAMES_KEY.format(session_id=session_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, raw)
        pipe.expire(key, FRAMES_TTL)
        await pipe.execute()
    await publish(STREAM_PREFIX + session_id, raw)
