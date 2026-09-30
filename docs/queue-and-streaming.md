# Queue & Streaming

This document explains how the backend enqueues jobs and streams results back to
the frontend via Redis. For the agentic side (orchestrator, subagents, message
log, state) see [agentic-pipeline.md](agentic-pipeline.md).

## Flow overview

```
Frontend                    Backend                      Worker
   │                          │                           │
   │  POST /create_chat_session                            │
   │─────────────────────────►│                           │
    │                          │  1. Create Session (DB)   │
    │                          │  2. SET pending:{id}      │
    │                          │     (clear old frames)    │
    │                          │     + LPUSH jobs:queue    │
    │  { session_id, job_id }  │──────────────────────────►│
    │◄─────────────────────────│  3. BRPOP jobs:queue      │
    │                          │                           │
    │  WS /ws/session/{id}     │                           │
    │════════════════════════►│                           │
    │                          │  4. Run orchestrator turn │
    │                          │  5. RPUSH stream:{id}:frames
    │                          │     + PUBLISH stream:{id} │
    │◄═════════════════════════│◄──────────────────────────│
    │   { type: "chat" }       │   (list = source of truth,│
    │◄═════════════════════════│    publish = wakeup)      │
    │   { type: "suggestions" }│                           │
    │◄═════════════════════════│◄──────────────────────────│
    │   { type: "end" }        │                           │
```

## Job queue (`jobs:queue`)

The backend pushes jobs to a Redis list. The worker block-pops them.

### Backend — enqueue

`POST /create_chat_session`, `POST /push_chat_message` and
`POST /submit_questionnaire_answers` all enqueue the same job shape
(`backend/main.py`):

```python
job = {
    "job_id": str(uuid4()),
    "session_id": session.id,
    "user_input": user_data.content,
}
await redis.lpush("jobs:queue", json.dumps(job))
```

The questionnaire endpoint differs in one way: `user_input` carries a JSON
payload (`{"kind": "questionnaire_answers", "answers": [{key, answer}, ...]}`)
instead of free text, so the worker can map the answers onto the questions by
key without LLM parsing (see `docs/questionnaire-tool.md`).

Every enqueue also records the in-flight message so other tabs can surface it:
the backend calls `mark_pending(session_id, content, type)` **before** the
`LPUSH` — so the worker can never finish (and clear the marker) before it was
set. `mark_pending` writes `pending:{session_id}` in Redis (5-min TTL), clears
the previous turn's `stream:{session_id}:frames` buffer and flips
`Session.status` to `PENDING`. The worker clears the key and sets the status
back to `ACTIVE` (or `FAILED`) once the job finishes — see
[In-flight tracking](#in-flight-tracking) below.

### Worker — dequeue

`worker/main.py` block-pops from the queue in its main loop and hands each job
to `process_job`:

```python
while not stop.is_set():
    result = await redis.brpop("jobs:queue", timeout=5)
    if result is None:
        continue
    _, raw = result
    job = json.loads(raw)
    await process_job(job, graph)
```

## Processing a job (`worker/agent.py`)

For each job the worker:

1. **Loads state** — reads `langgraph_state:{session_id}` from Redis. If absent,
   it rebuilds the message log from the session's chat history in the DB
   (ordered by `created_at`).
2. **Injects the user message** into the state and runs the graph.
3. **Saves state** back to Redis (24h TTL) and persists the produced messages to
   the `Message` table.
4. **Publishes** assistant results to the session's stream channel, followed by
   a `suggestions` frame and an `end` frame.

Every user message is persisted. The `Message.agent` column is `CHAT` for chat
messages and `TOOL` for tool messages; the tool-specific JSON shape lives in the
`content` column (`type` + extra fields). See
[agentic-pipeline.md](agentic-pipeline.md) for the full message formats.

### Error handling

If a job fails, the worker marks the session `FAILED` and publishes an error
frame to the session's stream channel:

```json
{"type": "error", "job_id": "…", "content": "Job … failed"}
```

The WebSocket forwards it to the frontend. Both backend endpoints return the
`job_id` in their responses so failures can be correlated.

## Streaming results (`stream:{session_id}`)

Each frame is written twice by the worker (`worker/helpers/events.py`):

- **`RPUSH stream:{session_id}:frames`** — a per-turn Redis list (5-min TTL,
  cleared by `mark_pending` when the next turn starts). This is the source of
  truth.
- **`PUBLISH stream:{session_id}`** — a wakeup for anyone already listening.

The backend WebSocket (`/ws/session/{session_id}`) **tails the list** with a
cursor and treats the pub/sub message only as a "read again" signal:

- It replays every frame from the cursor, so a client that connects *after* a
  fast turn already finished — or between two frames during a reconnect —
  still receives the whole reply instead of losing it (bare pub/sub would have
  dropped those messages).
- When the buffer is drained and `pending:{session_id}` is absent, it sends
  `end` and closes; while a job is still running it waits on the wakeup
  (1s poll fallback).
- Every send goes through a `_safe_send` helper that treats a client that
  disconnected mid-stream (e.g. a tab that was closed) as a normal close, so it
  never crashes the endpoint.

### Message protocol

Each frame streamed on `stream:{session_id}` is a JSON string:

| `type` | Payload | Description |
|---|---|---|
| `chat` | `content` | A chat reply |
| `questionnaire` | `content`, `questions`, `facts` | Questionnaire questions (rendered as a slide UI, one at a time) |
| `questionnaire_complete` | `content`, `context` | Acknowledges the answers received |
| `swot` | `content`, `sections`, `summary` | SWOT analysis result |
| `research` | `content` | Web search result |
| `suggestions` | `tools: [{name, description, example, suggestion}]` | "wanna try this next?" suggestions |
| `error` | `job_id`, `content` | Job failed; the session is marked `FAILED` |
| `end` | — | Signals the stream is finished; the WebSocket closes |

The frontend should render each frame as it arrives and stop when it receives
`end`. User-generated messages are not streamed (the client already has them).

## Session status

`Session.status` (`services/database/schema.prisma`) tracks lifecycle:

| Status | Meaning |
|---|---|
| `ACTIVE` | Default; no job in flight (idle or completed) |
| `PENDING` | A job for this session is in the queue or being processed |
| `FAILED` | The worker encountered an error processing a job for this session |

## In-flight tracking (`pending:{session_id}`)

Pub/sub is fire-and-forget and a session's messages only exist in the DB once
the worker has processed them. To let a *fresh* tab (or the session list) see
that the assistant is still working, the backend keeps a lightweight marker:

- **On enqueue**, `mark_pending(session_id, content, type)` writes
  `pending:{session_id}` = `{"content": …, "type": …}` (5-min TTL) and sets the
  session status to `PENDING`. `content` is the user's message verbatim, except
  for questionnaire answers where it's the same numbered summary the frontend
  echoes (`"1) answer"` lines).
- **`GET /get_messages`** returns the marker as a top-level `pending` field
  alongside `data`, so a fresh tab can render the in-flight user bubble and
  connect to the live stream without waiting for the worker.
- **The worker clears it** in `process_job`: on success it deletes the key and
  marks the session `ACTIVE`; on failure it deletes the key and marks it
  `FAILED`.
- **The WebSocket** no longer uses the marker to decide whether to connect: it
  always drains `stream:{session_id}:frames` (replaying a turn that finished
  between the POST and the socket opening), and only sends `end` + closes once
  the buffer is drained *and* the marker is absent.

Frontend behavior: when `GET /get_messages` returns a non-null `pending`, the
tab appends the optimistic user bubble (flagged `pending: true`), shows the
typing indicator, and connects the WebSocket to receive the result live. Sending
is blocked while `streaming` is true, so a busy session never gets a second
message injected mid-turn from another tab. If a turn ends (`end` frame)
without a single content frame having arrived on that socket, the tab
re-fetches `GET /get_messages` — the safety net that clears the optimistic
bubble and shows the reply from the DB when frames were lost entirely.

## Key considerations

- **Frames are buffered, then replayed** — pub/sub alone is fire-and-forget
  (published messages are lost when nobody is subscribed, e.g. the milliseconds
  between the POST returning and the socket opening). The
  `stream:{session_id}:frames` list covers that window for 5 minutes; the
  `Message` table remains the durable record beyond it.
- **One channel per session** — `stream:{session_id}` (and its `:frames` list)
  are unique per session. Multiple open tabs each connect their own WebSocket
  and independently replay/tail the same list; the frontend closes stale
  sockets before opening a new stream.
- **State persistence** — the state is stored at `langgraph_state:{session_id}`
  (24h TTL). If it's gone, the worker rebuilds the message log from the DB, so
  the conversation resumes instead of restarting.
- **Job IDs** — the backend generates a `job_id` per job and returns it in the
  API response; the worker includes it in error frames so failures can be
  correlated to a specific submission.
