# KapexAI

KapexAI is a business consultant chatbot. A user describes their business idea
and KapexAI helps them think it through: it asks a few questions, can research
topics on the web, can produce analyses like a SWOT breakdown, and can build
full dashboard reports (SWOT, market/competitor/financial/risk/scenario
analysis).

## How it works

The project is split into three parts that run together:

- **Backend** (`backend/`) - a FastAPI app that exposes the chat endpoints and
  streams responses to the frontend over a WebSocket.
- **Worker** (`worker/`) - a background process that picks up each user message
  and runs it through an orchestrator (planner → subagents → composer). Every
  message either gets a conversational reply, an inline tool card, another
  questionnaire round, or a new dashboard.
- **Services** (`services/`) - PostgreSQL for storing conversations and Redis
  for the job queue and real-time streaming.

## Subagents

Subagents are the capabilities the chatbot can use. They are registered in one
place (`worker/agents/registry.py`), so adding a new one means writing a class
and registering it.

Currently available:

- **Questionnaire** - asks targeted questions about the business idea to gather
  context before the assistant gives advice.
- **Web search** - researches a topic, a competitor, or a market on the web.
- **SWOT** - produces a SWOT (strengths, weaknesses, opportunities, threats)
  analysis.
- **Economics / Foresight** - economic data and scenario outlook for the
  business.
- **Finance / Indian finance** - financial calculations and analysis: returns,
  valuation, risk, equity metrics, SEC public-company filing lookups, and
  Indian calculators (SIP, EMI, tax). The finance agent powers 109 internal
  calculator functions (see `worker/tools/finance_calculators.py`); SEC lookups
  need `SEC_USER_AGENT` set.
- **Indian legal search / Case search / Issue register** - official Indian
  regulatory sources, Indian Kanoon case law, and a deterministically scored
  compliance issue register.
- **Astrology** - an astrology perspective (with disclaimer).

## Getting started

Requirements: Python 3.12+ and `uv`, plus a running PostgreSQL and Redis with
connection strings set in a `.env` file (`DATABASE_URL`, `REDIS_URL`, and API
keys for the LLM and search provider).

```sh
make install       # install dependencies
make generate      # generate the Prisma client
make migrate       # apply database migrations
```

Then start the two processes in separate terminals:

```sh
make dev-backend   # FastAPI server
make dev-worker    # background worker
```

Start the frontend in a third terminal:

```sh
cd frontend && npm install && npm run dev   # http://localhost:3000
```

Further reading: `docs/agentic-pipeline.md` (orchestrator, subagents,
dashboards, streaming) and `AGENTS.md` (contributor guide).
