# KapexAI — Agent Guide

## Workspace

uv workspace monorepo (Python >=3.12). Packages: `backend/`, `worker/`, `services/database/` (published as `db-service`), `services/redis-service/` (published as `redis-service`). `frontend/` is a placeholder (empty).

## Essential commands

| Command | What it does |
|---|---|
| `make install` | `uv sync` — install all workspace packages |
| `make generate` | `uv run prisma generate --schema=services/database/schema.prisma` — regenerate Prisma client after schema changes |
| `make migrate` | `uv run prisma migrate dev --schema=services/database/schema.prisma` — create & apply a new migration |
| `make dev-backend` | `uv run --package backend uvicorn backend.main:app --reload` — start FastAPI dev server |
| `make dev-worker` | `uv run --package worker python -m worker.main` — start worker |

## Dependency management

Use `uv add --package <pkg> <dep>` (never `pip install`). Example:
- `uv add --package backend "httpx>=0.27.0"`
- `uv add --package worker "redis>=5.0"`
- `uv add --dev "ruff"` (root dev dep)

`uv run` replaces virtualenv activation — it auto-finds the right environment.

## Testing

- Framework: `pytest` with `pytest-asyncio` for async tests
- Run backend tests: `cd backend && uv run pytest tests/ -v`
- Run worker tests: `PYTHONPATH=. uv run --package worker pytest worker/tests/ -v` (worker tests hit real DB + Redis)
- Test files: `backend/tests/test_*.py`, `worker/tests/test_*.py`

## Services

### `db-service` — PostgreSQL (Prisma)

- ORM: Prisma, schema at `services/database/schema.prisma`
- Provider: PostgreSQL
- Connection string via `DATABASE_URL` env var (`.env`)
- Shared client: `from db_service import db, connect_db, disconnect_db`
- After editing `schema.prisma`, run `make generate` then `make migrate`
- `.prisma/` is gitignored (generated Prisma client)

Schema models: `User`, `Session`, `Message` (with `Role`, `Agent`, `Status` enums). The `Agent` enum tracks which agent produced a message (`QUESTIONNAIRE`, `RESEARCH`, `REPORT`, `GUARDRAIL`, `CHAT`, `TOOL`).

### `redis-service` — Redis Cloud (redis-py)

- Provider: Redis Cloud via `redis-py` (standard TCP Redis, supports pub/sub)
- Env var: `REDIS_URL` (e.g. `redis://user:pass@host:port`)
- Shared client: `from redis_service import redis, connect_redis, disconnect_redis`
- `connect_redis()` / `disconnect_redis()` are **async** (`await` them)
- Client is created with `socket_timeout=None` — required so blocking commands like `brpop` (used by the worker) return `None` instead of racing with the 5s socket default and raising `TimeoutError`
- Backend connects Redis in its `lifespan` alongside the DB
- Supports pub/sub — used by the WebSocket endpoint for real-time streaming

Full docs at `docs/agentic-pipeline.md` (router/tools/message log), `docs/services.md`, and `docs/queue-and-streaming.md`.

## OpenCode

Custom commands in `.opencode/commands/`:
- `start-work` — syncs main with upstream and creates a feature branch
- `pr-prep` — analyzes changes, generates tests, drafts PR description

## State of project

Functional end-to-end pipeline with Google OAuth authentication. Backend pushes jobs to a Redis queue; the worker consumes them and runs a LangGraph **chat + tools** graph; results stream back to the frontend over WebSocket + Redis pub/sub. Auth endpoints (`/auth/google`, `/auth/google/callback`, `/auth/me`) are protected by JWT tokens. The frontend is still a placeholder.

## Architecture & data flow

1. **Backend** (`backend/main.py`) exposes REST endpoints that create sessions/messages and push jobs to Redis queue `jobs:queue`.
2. **Worker** (`worker/main.py`) polls `jobs:queue` with `brpop` (5s timeout), then runs the job through a compiled LangGraph graph.
3. **Graph** (`worker/agent.py`) is a `StateGraph` with a single `router` node that decides how to handle each user message:
   - `chat` — `chat_agent` (business-consultant persona) replies conversationally; the reply is saved as a `Message` and streamed.
   - `tool` — dispatch to a registered tool (see `worker/tools/registry.py`). Each tool returns message entries with its own JSON shape (`type` + extra fields); `tool_node` persists and streams them.
   - The **router** sends greetings/small talk to the chat agent (which stays strictly business-focused) and routes a shared business idea to the **questionnaire tool** to build context; while questions are pending, answers are routed back to it automatically.
   - Every chat/tool turn ends by streaming a `suggestions` event listing available tools (name + example + suggestion phrase) and an `end` event.
4. **Streaming** — each node publishes to pub/sub channel `stream:{session_id}`; the backend WebSocket endpoint `ws/session/{session_id}` forwards it to the client.
5. **State** — a **message log** (`messages`) is cached in Redis (`langgraph_state:{session_id}`, 24h TTL) and rebuilt from DB message history (ordered by `created_at`) via `worker/helpers/persistence.py`. Each log entry is `{role, agent, type, content, ...tool-specific fields}`.

## Key modules

| File | Description |
|---|---|
| `backend/main.py` | FastAPI app: `/health`, `/waitlist`, `/create_chat_session`, `/push_chat_message`, `/get_sessions`, `ws/session/{session_id}` |
| `backend/utils/jwt_utils.py` | JWT token creation and verification using python-jose (HS256, 7-day expiry) |
| `backend/middleware/auth.py` | FastAPI `get_current_user` dependency — extracts Bearer token, decodes JWT, fetches user from DB |
| `backend/routers/auth.py` | Google OAuth endpoints: `/auth/google`, `/auth/google/callback`, `/auth/me` |
| `backend/utils/db_utils.py` | Backend-side Prisma helpers (`get_user`, `get_session`, `get_all_sessions`) |
| `worker/main.py` | Async worker loop; polls Redis queue and dispatches jobs |
| `worker/agent.py` | LangGraph graph definition, state load/save, `process_job` |
| `worker/agents/` | `router_agent.py` (intent classifier), `chat_agent.py` (consultant chat) |
| `worker/helpers/persistence.py` | Prisma helpers + DB message-log rebuild for the worker |
| `worker/helpers/messages.py` | Message-log helpers (transcript, questionnaire state, business context) |
| `worker/helpers/events.py` | Pub/sub stream publishing helpers |
| `worker/tools/` | Plug-and-play tools: `base.py`, `registry.py`, `questionnaire_tool.py`, `swot_tool.py`, `web_search_tool.py` |
| `worker/prompts/` | LLM prompt templates per agent/tool |
| `worker/tools/tavily_search.py` | Tavily search tool used by `web_search_tool` |
| `worker/tests/` | `test_chat_tools.py` — queue/pub-sub, chat, tool, questionnaire, state-rebuild tests |
| `backend/tests/` | `test_main.py`, `test_jwt_utils.py`, `test_auth.py`, `test_middleware.py` |

Before running either service, ensure the Prisma client is generated: `make generate`.
