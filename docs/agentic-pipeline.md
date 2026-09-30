# Agentic Pipeline

This document explains how KapexAI's agentic pipeline works: how a user message
becomes a job, how the worker's plain-async **orchestrator** decides what to do,
how subagents produce messages and dashboards, and how everything is persisted
and streamed.

## High-level flow

```
Frontend                    Backend                      Worker
   │                          │                           │
   │  POST /create_chat_session  (or /push_chat_message)   │
   │─────────────────────────►│                           │
   │                          │  1. Create/find Session   │
   │                          │  2. LPUSH jobs:queue      │
   │  { session_id, job_id }  │──────────────────────────►│
   │◄─────────────────────────│  3. BRPOP jobs:queue      │
   │                          │                           │
   │  WS /ws/session/{id}     │                           │
   │════════════════════════►│                           │
   │                          │  4. Run the orchestrator  │
   │                          │  5. PUBLISH stream:{id}   │
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "chat" }       │   (via redis.publish)     │
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "suggestions" }│                           │
   │◄═════════════════════════│◄──────────────────────────│
   │   { type: "end" }        │                           │
```

The pipeline is *message-driven*: every turn starts with a user message and ends
with one or more assistant messages plus a `suggestions` frame. There is no
fixed multi-stage workflow — the planner decides per-message whether to chat,
run an inline tool card, conduct the questionnaire interview, or build a
dashboard.

## The orchestrator (`worker/orchestrator/`)

The worker is a plain async Python pipeline — the old LangGraph `StateGraph`
router/tool graph is gone (the `langgraph` dependency remains only for two
ReAct-style agents, `web_search` and `finance`). One job = one call to
`Orchestrator.handle(state)` (`worker/orchestrator/orchestrator.py`), which:

1. builds the shared `AgentContext` (session/user ids, user input, message log,
   business context, transcript),
2. asks the **planner** for a `Plan`,
3. runs the matching turn mode through the **composer**,
4. streams `suggestions` (when the questionnaire is complete) and `end`.

Three collaborators keep the responsibilities clean:

| Module | Responsibility |
|---|---|
| `planner.py` | Policy: deterministic gates + one LLM call → a sanitized `Plan` the executor can trust blindly |
| `composer.py` | The only code that persists messages or publishes stream frames |
| `dashboard_builder.py` | Turns subagent results into a dashboard document (deterministic draft + one narrative LLM pass) |

### Turn modes

- **`questionnaire`** — runs the `questionnaire` subagent. The interview owns
  its own message-log entries (the frontend slide-UI contract depends on their
  exact shapes), so the composer persists `result.entries` verbatim; only a
  crash falls back to a normal user-echo + card pair.
- **`inline`** — exactly one subagent runs and its result is rendered as a
  tool card (`user_echo` + `assistant_card`). Existing frontend card
  components keep reading the same fields: the card spreads `AgentResult.data`
  as sibling keys on the entry.
- **`chat`** — the default. The planner may attach zero or more *support*
  subagents (e.g. web research to ground the reply); their plain-text findings
  are passed to `ChatAgent` as a `notes` section. The turn persists
  `user_echo` + `assistant_chat` and never blocks on a tool.
- **`dashboard`** — all planned subagents run **in parallel**
  (`asyncio.gather`); their results feed `DashboardBuilder`, a `Dashboard` row
  is created, and the turn persists a short chat intro (`data["intro"]`) plus
  a `dashboard` reference entry. If every feed fails, the turn degrades to a
  chat apology instead of creating an empty shell.

Frame order on a dashboard turn: `chat` → `dashboard` → `suggestions` → `end`.

### The planner (`planner.py`)

The planner is the orchestrator's policy layer. **Deterministic gates run
first**, then one LLM call, then a sanitize pass:

1. **Pending questionnaire** — while the interview asked questions that are
   still unanswered, every message returns `Plan(mode="questionnaire")`
   *without calling the LLM* (answers, structured slide payloads, and
   clarifications all go back to the interview agent).
2. **LLM plan** — otherwise `PLAN_TEMPLATE | llm` reads the roster
   (`list_subagents()`), transcript and business context, and returns JSON:
   `{mode, subagents: [{name, query}], dashboard_kind, reply_instruction}`.
   Any failure (chain build, API, parse) logs and returns `{}` → chat.
3. **Sanitize** (`Plan` contract the executor can trust):
   - unknown `mode` → `chat`
   - `questionnaire` mode after the interview completed → `chat`
   - `inline` keeps **≤ 1** subagent; a `requires_context` subagent chosen
     before the questionnaire completes is redirected to the questionnaire;
     no valid subagent → `chat`
   - `dashboard` before the questionnaire completes → the questionnaire;
     no valid subagent → `chat`; unknown/missing `dashboard_kind` →
     `general` (label "Business Analysis")
   - before completion, `requires_context` subagents are dropped from plain
     chat turns (the reply simply lacks their findings)
   - unknown/questionnaire subagent names are dropped from the roster

Dashboard kinds: `swot`, `market_analysis`, `competitor_analysis`,
`financial_analysis`, `risk_analysis`, `scenario_analysis`, plus the
`general` fallback.

### The composer (`composer.py`)

- `run_subagent(name, query, ctx)` — looks the agent up, awaits it (sync
  agents run via `asyncio.to_thread`), and converts failures into a friendly
  `tool_error` card so one broken capability never takes the turn down.
  Rate limits are special: a 429 anywhere in the exception chain is re-raised
  so `process_job` can show its dedicated "API limit" reply.
- `commit(session_id, messages, entries)` — appends entries to the log,
  persists them via `add_message` (stripping `role`, `agent` and
  `dashboard_data` from the stored JSON), and streams every ASSISTANT entry.
  The USER echo (`type: "chat"`) is persisted but **never streamed** — the
  client already has what the user typed.
- `publish_suggestions(session_id, messages)` — streams the roster as a
  `suggestions` frame using the frame key `tools`
  (`{name, description, example, suggestion}` — `requires_context` is not
  exposed to the frontend). The `questionnaire` entry is left out once the
  interview has completed.

Envelope helpers: `user_echo`, `assistant_chat`, `assistant_card`.

### The dashboard builder (`dashboard_builder.py`)

Two steps produce the dashboard document:

1. **Deterministic draft** — each subagent result maps into generic sections
   (SWOT quadrants, `insights` → bullets, otherwise the plain text truncated),
   capped at 8 sections / 12 items / 6 000 chars; sources are deduped URLs
   (max 12). The dashboard always has content even if the LLM is down.
2. **One narrative LLM pass** — suggests `name`, `title`, `subtitle`,
   `intro`, `summary` and grounded `suggestions`. Suggestion provenance is
   enforced in Python: a `source_index` must be an integer in range of the
   real source list or the suggestion is labelled `origin: "ai"` instead of
   being dressed up as a citation. LLM failure falls back to the
   deterministic document (name capped at 80 chars, label default).

The document: `{kind, kind_label, name, title, subtitle, intro, summary,
sections, suggestions, sources}`.

## State: a message log

`worker/agent.py` keeps state intentionally minimal:

```python
class State(TypedDict):
    session_id: str
    user_id: str
    user_input: str
    messages: list[dict]   # the conversation log
```

(Legacy `intent`/`tool` keys are popped on load.) The conversation lives
entirely in `messages`, and every entry has its own JSON shape defined by the
agent that produced it:

```json
{ "role": "USER|ASSISTANT", "agent": "CHAT|TOOL|REPORT|...", "type": "...", "content": "...", ... }
```

### Loading & saving

- **Cache** — the full state is stored in Redis at `langgraph_state:{session_id}`
  with a 24h TTL (`save_state` / `load_state`).
- **Rebuild** — if the Redis state is gone, `build_state_from_db`
  (`worker/helpers/persistence.py`) reconstructs the log from the `Message`
  table, **ordered by `created_at` ascending**, so the conversation is rebuilt
  in sequence (a mid-questionnaire session correctly resumes where it left off).
- **Dashboard expansion** — `load_state` calls `expand_dashboards`, which
  batch-loads each referenced `Dashboard.data` and re-attaches it as
  `dashboard_data` (idempotently; a missing row leaves the bare reference).
- **Business profile** — `load_state` refetches the user's `BusinessProfile`
  and injects/replaces a `business_profile` log entry
  (`inject_business_profile`), so every turn sees fresh profile values.

### Persistence

Every log entry the composer produces is written to the `Message` table via
`add_message` (`worker/helpers/persistence.py`). The `Message.agent` column
uses the `Agent` enum: `CHAT` for chat/echo messages, `TOOL` for inline card
messages (the specific tool is recoverable from `content.type`), `REPORT` for
dashboard reference entries; `QUESTIONNAIRE`/`RESEARCH`/`GUARDRAIL` remain for
legacy rows.

## Subagents (`worker/agents/`)

Subagents are the plug-and-play capabilities. The orchestrator talks to them
in plain text: it asks a specific question (`query`) with the shared turn
`context` and gets an `AgentResult` back. Subagents never build the final
structured message, never touch the database and never publish to the stream.

### Contract (`base.py`)

```python
@dataclass
class AgentContext:
    session_id: str
    user_id: str
    user_input: str
    messages: list[dict]
    business_context: dict
    transcript: str

@dataclass
class AgentResult:
    text: str = ""                       # plain-text answer
    data: dict | None = None             # raw payload for the inline card
    message_type: str | None = None      # inline card type ("swot", "research", ...)
    sources: list[dict] = []             # {label, url} for dashboard attribution
    entries: list[dict] | None = None    # escape hatch: questionnaire owns its entries

class SubAgent:
    name: str = ""
    description: str = ""   # shown to the planner
    example: str = ""       # example user prompt
    suggestion: str = ""    # "wanna try this next?" phrase for the frontend
    requires_context: bool = False       # gated on questionnaire completion
    def run(self, query: str, ctx: AgentContext) -> AgentResult: ...
```

### Registry (`registry.py`)

Agents register once at import and are picked up by the planner, the chat
agent and the `suggestions` frame:

```python
register(QuestionnaireAgent())
register(SwotAgent())
register(WebSearchAgent())
# ... 11 total
```

### Built-in subagents

| Agent | `type` (assistant msg) | What it does | Needs questionnaire? |
|---|---|---|---|
| `questionnaire` | `questionnaire` / `questionnaire_complete` | Multi-turn: asks up to 5 targeted questions, then folds the answers into structured business context (slide UI in the frontend) | — |
| `swot` | `swot` | Generates a SWOT analysis as structured sections | yes (`requires_context`) |
| `web_search` | `research` | Live web research via a Tavily-powered react agent | yes (`requires_context`) |
| `economics` | `economics` | Economic indicators/data for the business | yes (`requires_context`) |
| `foresight` | `foresight` | Scenario outlook for the business | yes (`requires_context`) |
| `finance` | `finance` | Financial calculations and analysis (returns, valuation, risk, equity metrics, SEC public filings) via an internal react agent | no |
| `astrology` | `astrology` | Astrology perspective (with disclaimer) | no |
| `indian_legal_search` | `legal_research` | Discovers official Indian regulatory sources (Tavily over the `worker/helpers/indian_sources.py` domain allowlist) | no |
| `indian_case_search` | `case_search` | Indian Kanoon case-law search (needs `INDIANKANOON_API_TOKEN`) | no |
| `legal_issue_register` | `issue_register` | Deterministically scored Indian compliance issue register | no |
| `indian_finance` | `indian_finance` | Indian finance calculations (SIP, EMI, tax, …) | no |

Every subagent's `run(query, ctx)` receives the message log through `ctx`, so
it can use both the collected business context (`ctx.business_context`) and the
conversation transcript (`ctx.transcript`) alongside the current question.

### Subagent API tokens

- `TAVILY_API_KEY` — used by `web_search` and `indian_legal_search` (Tavily).
- `INDIANKANOON_API_TOKEN` — used by `indian_case_search` to query the Indian
  Kanoon API (https://indiankanoon.org/, token-based auth).

Both are read by the **worker**, the only service that executes subagent calls
(the backend only pushes jobs onto the Redis queue). They are loaded from the
**root `.env`** file at startup (`worker/llm.py` and the agent modules call
`load_dotenv(<repo-root>/.env)`) — so add them there, e.g.:

```bash
INDIANKANOON_API_TOKEN="your-real-indian-kanoon-token"
```

`.env.example` documents both variables. **Never commit a real token** — `.env`
is gitignored and anything committed (e.g. into `.env.example`) must be a
placeholder. When `INDIANKANOON_API_TOKEN` is missing the agent still responds
gracefully instead of failing the job: it emits a `missing_credentials`
message ("Sorry, this tool is not configured yet. …"), which the frontend
renders as a plain markdown reply. A wrong token surfaces a `tool_error`
message. Adding the token to the *frontend* environment has no effect — the
frontend never holds or sends these tokens.

### The LLM layer (`worker/llm.py`)

Every prompt chain is built with `get_llm(temperature)` — the single place
that decides which provider/model the worker uses. Configuration:

- `LLM_PROVIDER` — `openai` (default), `google`, or any OpenAI-compatible
  free tier: `groq`, `openrouter`, `mistral`, `ollama` (local, no key)
- `LLM_MODEL` — optional model override
- `LLM_BASE_URL` — optional endpoint override for the OpenAI-compatible
  providers (e.g. Cerebras, Together)
- `LLM_TIMEOUT` — per-call timeout in seconds (default 60), so a stalled
  provider can't hang a job and leave the session stuck `PENDING`
- API keys: `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` /
  `OPENROUTER_API_KEY` / `MISTRAL_API_KEY` (only the one matching
  `LLM_PROVIDER` must be set; `ollama` needs none)

Subagents, the planner, the chat agent and the dashboard narrative all go
through this helper, so switching providers is a `.env` change.

### The finance agent (`finance`)

`finance` is a single top-level agent that exposes **109 internal finance
functions** to its own LangGraph react agent (the same pattern `web_search`
uses with `tavily_search`). The 109 underlying tools live in
`worker/tools/finance_calculators.py` (60 calculators),
`equity_calculators.py` (43 equity models) and `finance_tools.py` (6
finance/SEC tools) and are **never** registered as KapexAI subagents — the
planner, chat agent and `suggestions` frame only ever see `finance`.

When a user asks a finance question, the react agent picks the right underlying
tool(s), extracts the arguments, calls them (one or several in sequence), and
writes a single answer. The agent is created once at construction and reused
across calls; its system prompt is rebuilt per request with the business context
and transcript. SEC lookups require the `SEC_USER_AGENT` environment variable;
missing config, bad CIKs, or HTTP failures surface as friendly `error` fields
(or a clear assistant message) instead of raw exceptions.

## Dashboards

Dashboards are long-lived report documents generated from one or more
subagent results.

- **Storage** — a `Dashboard` row (`id`, `sessionId`, `name`, `data` JSON,
  `created_at`). The row is the source of truth; chat messages only keep a
  reference (`dashboard_id` + `dashboard_name`) — never the full payload.
- **Backend** — `GET /get_dashboards?session_id=` (list), `GET /get_dashboard?dashboard_id=`
  (full payload), `GET /get_sessions` includes a per-session `dashboards`
  array for the sidebar. Deleting a session cascades to its dashboards.
- **Worker** — the `dashboard` turn mode creates the row and streams a
  `dashboard` frame carrying `dashboard_id`, `dashboard_name` and the expanded
  `dashboard_data` (stripped again by `commit` before the DB write).
- **Frontend** — a live `dashboard` frame auto-navigates to
  `/chat/:sessionId/dashboard/:dashboardId`; the persisted `dashboard` message
  renders as a "Dashboard ready" card with an **Open dashboard** button; the
  sidebar lists each session's dashboards under a toggle. The dashboard page
  shows the document (`DashboardTemplate`), a tab bar across the session's
  dashboards, and a back link (`/chat?session=…`) that restores the
  conversation.

## Message formats

Each agent owns the JSON format of the messages it emits. The `type` field
distinguishes them; extra fields carry agent-specific data. The composer's
USER echo is always `type: "chat"`; only the questionnaire agent persists its
own USER entries (`questionnaire_start`, `questionnaire_answer`).

### Chat (`agent: "CHAT"`)

| role | `type` | extra fields |
|---|---|---|
| USER | `chat` | `content` |
| ASSISTANT | `chat` | `content` |

### Questionnaire (`agent: "QUESTIONNAIRE"` / legacy `TOOL`)

| role | `type` | extra fields |
|---|---|---|
| USER | `questionnaire_start` | `content` (the business idea) |
| ASSISTANT | `questionnaire` | `content`, `questions: [{key, question}]`, `facts: {...}` |
| USER | `questionnaire_answer` | `content` (numbered summary), `answers: {...}` |
| ASSISTANT | `questionnaire_complete` | `content`, `context: {...}` (the collected business context) |

Answers are collected by the frontend's **slide questionnaire** (one question at
a time, then submit). The frontend posts them as structured `{key, answer}`
pairs to `POST /submit_questionnaire_answers`; the worker folds them into the
context by key (no LLM parsing). Each non-empty answer is still validated
per-question, so gibberish like `"asdf"`/`"hehe"` is rejected and re-asked
instead of entering the business context. A user can still type answers
free-form in the composer — the worker falls back to LLM validation + parsing
for that path.

The `context` from `questionnaire_complete` is what the chat agent and the
context-gated agents use as *business context* for later turns. Beyond that,
`swot` and `web_search` also receive the **full message transcript**
(`format_transcript` in `worker/helpers/messages.py`) and are told to ground
their output in it, so analysis and research reflect everything the user has
shared, not just the latest request.

### SWOT (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `swot` | `content` (markdown), `sections: {strengths, weaknesses, opportunities, threats}`, `summary` |

### Web search (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `research` | `content` |

The research prompt asks for a **concise summary (~300 words)** using short
bullets, ending with a short **"Next steps"** section that poses **2-3 specific
follow-up questions** to the user (e.g. about their competitors, target
demographics, or pricing) so the conversation keeps moving after the result.

### Indian legal search (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `legal_research` | `content`, `query`, `results: [{title, source_url, source_type, authority, document_type, jurisdiction, publication_date, effective_date, relevant_sections, summary, citation}]`, `disclaimer` |

Results are grounded in Python against the allowlist in
`worker/helpers/indian_sources.py`: `source_url` is only ever a URL that was
actually retrieved, `source_type` (`official`/`third_party`) and `authority`
come from the domain allowlist, and official results are listed first.

### Indian case search (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `case_search` | `content`, `query`, `cases: [{case_name, url, court, date, summary, citation}]`, `disclaimer` |

`sourced from Indian Kanoon` — a third-party database, never presented as an
official court record.

### Issue register (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `issue_register` | `content`, `issues: [{title, basis, grounded_in, explanation, mitigation, likelihood, severity, urgency, priority_score, priority}]`, `disclaimer` |

Priorities are recomputed deterministically from `likelihood × severity ×
urgency` (critical/high/medium/low); likelihood/severity/urgency from the LLM
are clamped to 1–5.

Any of the three legal agents may instead emit an ASSISTANT entry of type
`missing_credentials` (when an API token is not configured — see "Subagent
API tokens") or `tool_error` (when the upstream service fails).

### Dashboard reference (`agent: "REPORT"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `dashboard` | `content` ("{name} generated"), `dashboard_id`, `dashboard_name` (+ `dashboard_data` in state/stream only — stripped from the DB row) |

### Finance (`agent: "TOOL"`)

| role | `type` | extra fields |
|---|---|---|
| ASSISTANT | `finance` | `content` (the computed answer, markdown) |

## Streaming protocol

Each turn publishes JSON frames to the pub/sub channel `stream:{session_id}`
(`worker/helpers/events.py`). The backend WebSocket endpoint
`ws/session/{session_id}` forwards them verbatim to the frontend.

| `type` | Payload | Description |
|---|---|---|
| `chat` | `content` | A chat reply |
| `questionnaire` | `content`, `questions`, `facts` | Questionnaire questions (rendered as a slide UI, one at a time) |
| `questionnaire_complete` | `content`, `context` | Acknowledges the collected answers |
| `swot` | `content`, `sections`, `summary` | SWOT analysis result |
| `research` | `content` | Web search result |
| `economics` / `foresight` | `content`, `data`, `source` | Data-tool result |
| `astrology` | `content`, `insights`, `disclaimer` | Astrology perspective |
| `legal_research` | `content`, `query`, `results`, `disclaimer` | Indian regulatory search result |
| `case_search` | `content`, `query`, `cases`, `disclaimer` | Indian Kanoon case-law search result |
| `issue_register` | `content`, `issues`, `disclaimer` | Compliance issue register result |
| `finance` / `indian_finance` | `content`, ... | Finance calculation / analysis result (rendered as markdown) |
| `dashboard` | `content`, `dashboard_id`, `dashboard_name`, `dashboard_data` | A dashboard was created — the frontend auto-opens the dashboard page |
| `suggestions` | `tools: [{name, description, example, suggestion}]` | "wanna try this next?" — the user can pick one to trigger a subagent |
| `end` | — | Signals the turn is finished |
| `error` | `job_id`, `content` | The job failed; the session is marked `FAILED` |

USER echoes (always `type: "chat"`) are persisted but **not** streamed — the
client already has what the user typed.

## Adding a new subagent

1. Create `worker/agents/<name>_agent.py` with a class that subclasses
   `SubAgent`.
2. Set `name`, `description`, `example`, `suggestion` (and
   `requires_context = True` if it needs the completed questionnaire's
   business context).
3. Implement `run(query, ctx)` → an `AgentResult` (sync or async). Put the
   inline card payload in `data` + `message_type`, and any grounded URLs in
   `sources` so dashboards can attribute them.
4. Register it in `worker/agents/registry.py`.
5. If it emits a new inline card type, add a frontend card component in
   `frontend/src/components/messages/` and register it in `index.tsx`
   (unknown types fall back to plain markdown).

That's it — the planner prompt, chat context and `suggestions` frame all read
from the registry, so the new agent is immediately visible to the model and the
frontend.

For capabilities with many sub-operations, follow the `finance` agent instead:
keep the sub-functions as internal LangChain tools in dedicated modules under
`worker/tools/`, bind them all to one react agent inside the agent, and
register only the single top-level agent.

## Error handling

- A failing subagent becomes a friendly `tool_error` card (the turn
  continues); a 429 anywhere in the exception chain is re-raised instead so
  `process_job` can show the dedicated "API limit" reply.
- If the job itself fails, `process_job` marks the session `FAILED` and
  publishes an `error` frame containing the `job_id` so the failure can be
  correlated with the submission that caused it.

## Key considerations

- **Inline turns run one subagent** — the planner caps `inline` mode at a
  single card. Deeper analysis becomes a `dashboard` (all feeds run in
  parallel), and chat turns may consult multiple *support* subagents whose
  findings are folded into the chat reply's `notes`.
- **Pub/sub is fire-and-forget** — if no WebSocket is connected, stream frames
  are lost; the DB `Message` log is the durable record (and dashboards live in
  their own table).
- **Per-session serialization** — the queue is global, so two jobs for the same
  session are processed in arrival order by the single worker; with multiple
  workers you would need per-session locking.
- **The questionnaire runs once, and gates context subagents** — the planner
  sends it a shared business idea (and any messages while questions are
  pending). Until it completes, `requires_context` subagents (`swot`,
  `web_search`, `economics`, `foresight`) and `dashboard` mode are redirected
  to it; once answered it never blocks again. Greetings and small talk go to
  `chat` instead.
