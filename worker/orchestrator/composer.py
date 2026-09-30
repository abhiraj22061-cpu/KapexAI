"""Turn composition: runs subagents and turns their results into message-log
entries, database rows and stream frames.

The composer is the only place that persists messages or publishes to the
stream — subagents and the planner stay pure, which keeps them easy to test.
"""

import asyncio
import inspect
import logging

import httpx

from worker.agents.base import AgentContext, AgentResult
from worker.agents.registry import get_subagent, list_subagents
from worker.helpers.events import publish_stream
from worker.helpers.messages import append_message, questionnaire_complete
from worker.helpers.persistence import add_message

logger = logging.getLogger(__name__)

# `dashboard_data` lives in the in-memory/stream copy of a dashboard entry but
# is stripped from the stored JSON — the database row is the source of truth
# and `expand_dashboards` re-attaches it on load.
_EXPANDED_KEYS = ("role", "agent", "dashboard_data")


class Composer:
    async def run_subagent(self, name: str, query: str, ctx: AgentContext) -> AgentResult:
        """Runs one subagent, converting failures into a friendly error card so
        a single broken capability never takes the whole turn down. Rate limits
        are re-raised so the job layer can show its dedicated API-limit reply."""
        agent = get_subagent(name)
        if agent is None:
            return AgentResult(
                text=f"I don't have a \"{name}\" capability.", message_type="tool_error"
            )
        try:
            if inspect.iscoroutinefunction(agent.run):
                result = await agent.run(query, ctx)
            else:
                result = await asyncio.to_thread(agent.run, query, ctx)
        except Exception as exc:
            limited = _rate_limit_error(exc)
            if limited is not None:
                # Surface the 429 itself so the job layer shows its dedicated
                # API-limit reply even when the limit is wrapped by an SDK.
                if limited is exc:
                    raise
                raise limited from exc
            logger.exception("Subagent %s failed", name)
            return _failure(_detail(exc))
        return result if isinstance(result, AgentResult) else AgentResult(text=str(result))

    async def commit(self, session_id: str, messages: list[dict], entries: list[dict]) -> list[dict]:
        """Appends entries to the message log, persists them (without expanded
        data) and streams the assistant ones — same contract the old tool node
        had, minus the routing knowledge."""
        out = list(messages)
        for entry in entries:
            out = append_message(out, entry)
            content = {k: v for k, v in entry.items() if k not in _EXPANDED_KEYS}
            await add_message(session_id, entry.get("role", "ASSISTANT"), entry.get("agent", "CHAT"), content)
            if entry.get("role") == "ASSISTANT":
                await publish_stream(session_id, content)
        return out

    async def publish_suggestions(self, session_id: str, messages: list[dict]) -> None:
        """Streams the available suggestions. The questionnaire is left out once
        completed — re-offering it would be pointless."""
        tools = list_subagents()
        if questionnaire_complete(messages):
            tools = [t for t in tools if t["name"] != "questionnaire"]
        await publish_stream(session_id, {"type": "suggestions", "tools": tools})


def _rate_limit_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    """Walks the cause chain for a 429, mirroring the job layer's detection so
    a rate limit nested inside an SDK error still surfaces as the API-limit
    reply instead of a generic error card."""
    err: BaseException | None = exc
    while err is not None:
        if isinstance(err, httpx.HTTPStatusError) and err.response.status_code == 429:
            return err
        err = err.__cause__ or err.__context__
    return None


def _detail(exc: BaseException) -> str:
    return str(exc).strip() or "unexpected error"


def _failure(detail: str) -> AgentResult:
    return AgentResult(
        text=f"I could not complete that right now ({detail}). Please try again.",
        message_type="tool_error",
    )


def user_echo(text: str) -> dict:
    return {"role": "USER", "agent": "CHAT", "type": "chat", "content": text}


def assistant_card(result: AgentResult) -> dict:
    """Renders an inline subagent result as its tool card, spreading the raw
    data payload as sibling keys exactly as the old tools did (so existing
    frontend cards keep reading the same fields)."""
    entry = {
        "role": "ASSISTANT",
        "agent": "TOOL",
        "type": result.message_type or "chat",
        "content": result.text,
    }
    if result.data:
        entry.update({k: v for k, v in result.data.items() if k not in _EXPANDED_KEYS})
    return entry


def assistant_chat(content: str) -> dict:
    return {"role": "ASSISTANT", "agent": "CHAT", "type": "chat", "content": content}
