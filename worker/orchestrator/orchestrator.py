"""The central orchestrator: plans the turn, runs subagents, composes the
message log and streams frames back to the user.

Replaces the old LangGraph router/tool graph with one plain async `handle`
call per job. The planner decides *what* to do, the composer handles *how it
is persisted and streamed*, and this module sequences the turn modes.
"""

import asyncio
import logging

from worker.agents.base import AgentContext, AgentResult
from worker.agents.chat_agent import ChatAgent
from worker.agents.registry import list_subagents
from worker.helpers.events import publish_stream
from worker.helpers.messages import (
    business_context,
    format_transcript,
    questionnaire_complete,
)
from worker.helpers.persistence import create_dashboard
from worker.orchestrator.composer import (
    Composer,
    assistant_card,
    assistant_chat,
    user_echo,
)
from worker.orchestrator.dashboard_builder import DASHBOARD_LABELS, DashboardBuilder
from worker.orchestrator.planner import Plan, Planner

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self.planner = Planner()
        self.composer = Composer()
        self.chat = ChatAgent()
        self.builder = DashboardBuilder()

    async def handle(self, state: dict) -> dict:
        """Runs one user turn end-to-end and returns the updated state."""
        messages = list(state.get("messages") or [])
        ctx = AgentContext(
            session_id=str(state.get("session_id") or ""),
            user_id=str(state.get("user_id") or ""),
            user_input=str(state.get("user_input") or ""),
            messages=messages,
            business_context=business_context(messages),
            transcript=format_transcript(messages),
        )

        plan = await self.planner.plan(ctx.user_input, messages)

        if plan.mode == "questionnaire":
            messages = await self._questionnaire(ctx)
        elif plan.mode == "inline":
            messages = await self._inline(plan, ctx)
        elif plan.mode == "dashboard":
            messages = await self._dashboard(plan, ctx)
        else:
            messages = await self._chat(plan, ctx)

        if questionnaire_complete(messages):
            await self.composer.publish_suggestions(ctx.session_id, messages)
        await publish_stream(ctx.session_id, {"type": "end"})
        return {**state, "messages": messages}

    # ── turn modes ───────────────────────────────────────────────

    async def _questionnaire(self, ctx: AgentContext) -> list[dict]:
        result = await self.composer.run_subagent("questionnaire", ctx.user_input, ctx)
        entries = result.entries
        if entries is None:
            # The interview owns its own USER/ASSISTANT entries (the slide-UI
            # contract depends on them); only a crash falls back to a card.
            entries = [user_echo(ctx.user_input), assistant_card(result)]
        return await self.composer.commit(ctx.session_id, ctx.messages, entries)

    async def _inline(self, plan: Plan, ctx: AgentContext) -> list[dict]:
        sub = plan.subagents[0]
        result = await self.composer.run_subagent(sub["name"], sub["query"], ctx)
        if result.entries is not None:
            entries = result.entries
        else:
            entries = [user_echo(ctx.user_input), assistant_card(result)]
        return await self.composer.commit(ctx.session_id, ctx.messages, entries)

    async def _chat(self, plan: Plan, ctx: AgentContext) -> list[dict]:
        notes = ""
        if plan.reply_instruction:
            notes += f"Reply guidance from the orchestrator: {plan.reply_instruction}\n\n"

        results = []
        for sub in plan.subagents:
            results.append((sub, await self.composer.run_subagent(sub["name"], sub["query"], ctx)))

        sections = []
        for sub, result in results:
            if result.entries is not None:
                continue
            if result.message_type == "tool_error":
                sections.append(f"(gathering {sub['name']} failed: {result.text})")
                continue
            sections.append(f"### {sub['name']}\n{result.text}")
        if sections:
            notes += "## Findings\n\n" + "\n\n".join(sections)

        reply = await self.chat.run(
            ctx.user_input,
            ctx.transcript,
            ctx.business_context,
            list_subagents(),
            notes,
        )
        entries = [user_echo(ctx.user_input), assistant_chat(reply)]
        return await self.composer.commit(ctx.session_id, ctx.messages, entries)

    async def _dashboard(self, plan: Plan, ctx: AgentContext) -> list[dict]:
        results = await asyncio.gather(
            *[
                self.composer.run_subagent(sub["name"], sub["query"], ctx)
                for sub in plan.subagents
            ]
        )
        gathered = [
            (sub["name"], result)
            for sub, result in zip(plan.subagents, results)
            if result.entries is None and result.message_type != "tool_error"
        ]

        if not gathered:
            # Every feed failed — say so instead of creating an empty shell.
            entries = [
                user_echo(ctx.user_input),
                assistant_chat(
                    "I couldn't gather the data for that dashboard right now. "
                    "Please try again in a moment."
                ),
            ]
            return await self.composer.commit(ctx.session_id, ctx.messages, entries)

        data = await self.builder.build(
            kind=plan.dashboard_kind or "general",
            request=ctx.user_input,
            context=ctx.business_context,
            results=gathered,
        )
        dashboard = await create_dashboard(ctx.session_id, data["name"], data)

        entries = [
            user_echo(ctx.user_input),
            assistant_chat(data["intro"]),
            {
                "role": "ASSISTANT",
                "agent": "REPORT",
                "type": "dashboard",
                "content": f"{data['name']} generated",
                "dashboard_id": dashboard.id,
                "dashboard_name": data["name"],
                # Expanded copy for this session's state/stream; stripped from
                # the stored JSON (the dashboard row is the source of truth).
                "dashboard_data": data,
            },
        ]
        return await self.composer.commit(ctx.session_id, ctx.messages, entries)


__all__ = ["DASHBOARD_LABELS", "AgentResult", "Orchestrator"]
