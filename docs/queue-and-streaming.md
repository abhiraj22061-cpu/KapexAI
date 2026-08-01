# Queue & Streaming

This document explains how the backend enqueues jobs and streams results back to the frontend via Redis.

## Flow overview

```
Frontend                    Backend                      Worker
   │                          │                           │
   │  POST /create_chat_session                            │
   │─────────────────────────►│                           │
   │                          │  1. Create Session (DB)   │
   │                          │  2. LPUSH jobs:queue      │
   │  { session_id }          │──────────────────────────►│
   │◄─────────────────────────│  3. BRPOP jobs:queue      │
   │                          │                           │
   │  WS /ws/session/{id}     │                           │
   │════════════════════════►│                           │
   │                          │  4. Run langgraph workflow│
   │                          │  5. PUBLISH stream:{id}   │
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "questionnaire" }   │   (via redis.publish)│
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "report" }     │                           │
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "end" }        │                           │
```

## Job queue (`jobs:queue`)

The backend pushes jobs to a Redis list. The worker block-pops them.

### Backend — enqueue

`POST /create_chat_session` and `POST /push_chat_message` both enqueue the same
job shape (`backend/main.py`):

```python
job = {
    "job_id": str(uuid4()),
    "session_id": session.id,
    "user_input": user_data.content,
}
await redis.lpush("jobs:queue", json.dumps(job))
```

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

## Worker workflow (`worker/agent.py`)

The worker runs a langgraph state machine with four agents:
**questionnaire**, **research**, **report**, **guardrail**.

For each job it:

1. **Loads state** — reads `langgraph_state:{session_id}` from Redis. If absent,
   it rebuilds state from the session's chat history in the DB (so the graph
   knows where to resume from).
2. **Injects the user message** into the state and runs the graph.
3. **Saves state** back to Redis (24h TTL) and persists the messages below to
   the DB.
4. **Publishes** every stage result to the session's stream channel.

### Message persistence

Only **user messages** and the **final guardrail-checked report** are stored in
the `Message` table (`worker/agents/questionnaire_agent.py`,
`worker/agent.py`):

| `Message.agent` | `role` | `content` shape |
|---|---|---|
| `QUESTIONNAIRE` | `USER` | `{"type": "idea", "content": ..., "facts": {...}}` |
| `QUESTIONNAIRE` | `USER` | `{"type": "answers", "answers": {...}}` |
| `REPORT` | `ASSISTANT` | `{"type": "report", "content": ...}` |

The intermediate research/report/guardrail results are streamed live but **not**
persisted. `build_state_from_db` (used only when Redis state is gone) rebuilds
state from these three message types and is order-insensitive.

### Error handling

If a job fails, the worker marks the session `FAILED` and publishes an error
frame to the session's stream channel:

```json
{"type": "error", "job_id": "…", "content": "Job … failed"}
```

The WebSocket forwards it to the frontend. Both backend endpoints return the
`job_id` in their responses so failures can be correlated.

The questionnaire phase:

- The **first message** of a session is the business idea. The questionnaire
  agent extracts everything it can from it and generates **up to 5 deep
  questions**, presented to the user **all at once**.
- The user's next message answers all questions in one go; the agent parses the
  answers, then the workflow proceeds: `research → report → guardrail → end`.

## Streaming results (`stream:{session_id}`)

The worker publishes to the session's pub/sub channel. The backend WebSocket
(`/ws/session/{session_id}`) subscribes and forwards each frame to the frontend.

### Message protocol

Each message published to `stream:{session_id}` is a JSON string:

| `type` | Payload | Description |
|---|---|---|
| `questionnaire` | `questions: [{key, question}]`, `content` | All questions, presented at once |
| `questionnaire_complete` | `content` | Acknowledges the answers received |
| `research` | `content` | Research summary |
| `report` | `content` | Generated business report |
| `guardrail` | `content` | Guardrail check result |
| `error` | `job_id`, `content` | Job failed; the session is marked `FAILED` |
| `end` | — | Signals the stream is finished; the WebSocket closes |

The frontend should render each stage as it arrives and stop when it receives
`end`.

## Session status

`Session.status` (`services/database/schema.prisma`) tracks lifecycle:

| Status | Meaning |
|---|---|
| `ACTIVE` | Default; the worker is processing or has completed |
| `FAILED` | The worker encountered an error processing a job for this session |

## Key considerations

- **Pub/sub is fire-and-forget** — if no WebSocket is connected, published messages
  are lost. Only user messages and the final report are persisted to the DB.
- **One channel per session** — `stream:{session_id}` is unique per session. Only
  one WebSocket client should connect per session.
- **State persistence** — the langgraph state is stored at
  `langgraph_state:{session_id}` (24h TTL). If it's gone, the worker rebuilds it
  from the DB messages, so the workflow resumes instead of restarting.
- **Job IDs** — the backend generates a `job_id` per job and returns it in the
  API response; the worker includes it in error frames so failures can be
  correlated to a specific submission.
