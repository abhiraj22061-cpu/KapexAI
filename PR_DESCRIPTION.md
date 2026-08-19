## 🚀 Pull Request: Add economics & foresight analysis tools + HTTP caching

### 📝 Summary of Changes
Adds two context-gated analysis tools — `economics` (live World Bank / BLS / exchange-rate data) and `foresight` (scenario matrix or JPL Horizons ephemeris) — to the LangGraph pipeline, plus a Redis-backed HTTP+TTL cache to dedupe external API calls. Includes matching frontend message cards, full test coverage, and updated agent docs.

### 🛠️ Key Modifications
- **Worker**:
  - `worker/tools/economics_tool.py` (new) — plans, fetches, and summarizes live macroeconomic data (World Bank indicators, BLS time series, Frankfurter exchange rates)
  - `worker/tools/foresight_tool.py` (new) — scenario planning (probabilities validated to sum to 1) or raw ephemeris output, with forecast-horizon guardrails
  - `worker/helpers/http_cache.py` (new) — Redis TTL cache (`tool_cache:{sha256}`) with retry/backoff and error mapping
  - `worker/prompts/economics.py`, `worker/prompts/foresight.py` (new) — tool planners/summarizers
  - `worker/tools/registry.py` — registers both tools with `requires_context=True` (gated until questionnaire completes); `worker/pyproject.toml` — adds `httpx`
  - `worker/tests/test_chat_tools.py` — economics/foresight routing, gating, history/data, scenario validation, and **4 new `http_cache` tests**
- **Frontend**:
  - `src/components/messages/EconomicsCard.tsx`, `ForesightCard.tsx` (new) — tool result cards; registered in `index.tsx`
  - `src/lib/types.ts` — `economics` / `foresight` `StreamFrame` variants; `src/styles/chat.css` — card styles
- **Docs**: `AGENTS.md` — documents the new tools, `http_cache`, and the `uv sync --all-packages` gotcha

### 🆕 Follow-up changes
- **Tool-first routing** (`worker/prompts/router.py`) — the router now sends factual/data/research questions to the matching tool (`economics` for GDP/inflation/World Bank country profiles, `web_search` for current info, `swot`/`foresight` for business analysis) instead of only chat. The chatbot is no longer restricted to business-only replies.
- **Chat fallback** (`worker/prompts/chat.py`) — general questions are answered conversationally when no tool matches, instead of being declined.
- **Windows runtime fix** (`worker/main.py`) — worker now starts on Windows (`loop.add_signal_handler` is unsupported there; falls back to `signal.signal`).
- **New tests** — router prompt/tool-routing regression + end-to-end World Bank country-profile answer via the `economics` tool.

### 🧪 Test & Quality Report
- **Auto-Generated Tests**: Yes — 4 new tests for `worker/helpers/http_cache.py` (cache-key stability, Redis TTL caching, transient-status retry, error mapping), appended to `worker/tests/test_chat_tools.py`
- **Execution Command**: `uv run --package worker pytest worker/tests/test_chat_tools.py -q` · `/.venv/bin/python -m pytest backend/tests/ -q` · `cd frontend && npm run build`
- **Status**: 🟢 PASSED
- **Details**:
  ```text
  worker/tests/test_chat_tools.py  35 passed in 229.21s
  backend/tests/                   88 passed in  1.90s
  frontend build (tsc -b + vite)   ✓ 323 modules transformed
  ```