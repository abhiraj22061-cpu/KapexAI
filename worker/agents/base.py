"""Subagent contract: the orchestrator talks to subagents in plain text.

Every capability that used to be a "tool" is now a subagent: the orchestrator
asks it a specific question (``query``) with the shared turn ``context`` and
gets back an :class:`AgentResult` — a plain-text answer plus optional raw
data/sources. Subagents never build the final structured message, never touch
the database and never publish to the stream; that is the orchestrator's job.
"""

from dataclasses import dataclass, field


@dataclass
class AgentContext:
    """Everything a subagent may need about the current turn."""

    session_id: str
    user_id: str
    user_input: str
    messages: list[dict]
    business_context: dict
    transcript: str


@dataclass
class AgentResult:
    """A subagent's answer.

    ``text`` is the plain-text response the orchestrator (and the chat agent)
    can reason over. ``data`` carries the raw structured payload when the
    subagent already has it (SWOT sections, legal results, ...); ``message_type``
    says which inline card type it maps to. ``sources`` lists grounded
    ``{label, url}`` references used for dashboard attribution.

    ``entries`` is an escape hatch for the questionnaire agent, which owns its
    own message-log entries (the slide-UI contract depends on their exact
    shapes); when set, the composer persists them verbatim.
    """

    text: str = ""
    data: dict | None = None
    message_type: str | None = None
    sources: list[dict] = field(default_factory=list)
    entries: list[dict] | None = None


class SubAgent:
    """Base class for subagents.

    To add a capability: subclass, set ``name``, ``description``, ``example``
    and ``suggestion``, implement ``run``, and register it in
    ``worker/agents/registry.py``.
    """

    name: str = ""
    description: str = ""
    example: str = ""
    suggestion: str = ""
    # True when the subagent needs the completed questionnaire's business
    # context (e.g. SWOT, research). The planner refuses to select such
    # subagents until the questionnaire has been completed.
    requires_context: bool = False

    def run(self, query: str, ctx: AgentContext) -> AgentResult:
        """Answer the orchestrator's question. May be sync or async."""
        raise NotImplementedError
