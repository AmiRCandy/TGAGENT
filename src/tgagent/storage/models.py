"""Records that cross the persistence boundary.

Plain dataclasses rather than ORM entities: the storage layer is deliberately
thin, and keeping the records free of database machinery is what makes the
repository protocols swappable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class MessageRole(StrEnum):
    """Who produced a turn in an agent conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


@dataclass(slots=True)
class Conversation:
    """A thread of interaction between the operator and the agent."""

    id: str = field(default_factory=_new_id)
    title: str = ""
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StoredMessage:
    """One persisted turn.

    ``content`` holds the provider-neutral message payload (text plus any tool
    calls / results), so a conversation can be replayed against a different
    provider than the one that produced it.
    """

    id: str = field(default_factory=_new_id)
    conversation_id: str = ""
    role: MessageRole = MessageRole.USER
    content: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    token_estimate: int = 0


@dataclass(slots=True)
class MemoryFact:
    """A durable fact or preference the agent should remember across runs."""

    id: str = field(default_factory=_new_id)
    key: str = ""
    value: str = ""
    category: str = "general"
    #: Where this came from. Facts derived from Telegram content are marked so a
    #: reviewer can tell operator-stated preferences from model-inferred ones.
    source: str = "agent"
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


class ScheduleKind(StrEnum):
    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class ScheduledTask:
    """A recurring or one-shot agent run.

    The task is *data*, not a pickled callable: an id, a schedule, and a prompt.
    That is what lets it survive restarts and code upgrades intact.
    """

    id: str = field(default_factory=_new_id)
    name: str = ""
    prompt: str = ""
    kind: ScheduleKind = ScheduleKind.CRON
    #: Cron expression, seconds (interval), or ISO-8601 timestamp (once).
    expression: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: TaskStatus | None = None
    last_error: str | None = None
    run_count: int = 0
    created_at: datetime = field(default_factory=_now)
    #: Per-task policy overrides, e.g. forcing read-only for a summary job.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatWatch:
    """A standing instruction to answer one chat on the owner's behalf.

    Like :class:`ScheduledTask`, it is *data*: a chat, an instruction, and the
    limits that bound it. What fires it is not a clock but somebody else's
    message, which is why every field below except the instruction exists to say
    *when to stop* — a watch that outlives the reason it was created is the
    failure mode worth designing against.
    """

    id: str = field(default_factory=_new_id)
    #: Telegram's *marked* chat id, as events report it: negative for groups and
    #: channels, positive for a private chat. Matching depends on this being the
    #: same form the bridge sees.
    chat_id: int = 0
    chat_title: str = ""
    #: The operator's standing instruction, in their words.
    instruction: str = ""
    #: User ids whose messages trigger it. Empty means anyone but the owner,
    #: which is the whole chat in a private conversation.
    senders: list[int] = field(default_factory=list)
    enabled: bool = True
    created_at: datetime = field(default_factory=_now)
    #: When it stops on its own. Never ``None`` in practice — the tool applies a
    #: default — but nullable so a deliberate forever-watch is expressible.
    expires_at: datetime | None = None
    max_replies: int = 20
    reply_count: int = 0
    last_reply_at: datetime | None = None
    #: Why it is no longer enabled, for the operator asking "what happened?".
    stopped_because: str | None = None
    #: Conversation the replies continue, plus provenance.
    metadata: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def exhausted(self) -> bool:
        return self.reply_count >= self.max_replies

    def matches_sender(self, sender_id: int | None) -> bool:
        return not self.senders or (sender_id is not None and sender_id in self.senders)


@dataclass(slots=True)
class AuditEntry:
    """One security-relevant event.

    Written for every Telegram call the gateway sees, allowed or not, plus
    sandbox executions and confirmation outcomes. This is the record an operator
    reads when asking "what did it actually do?".
    """

    id: str = field(default_factory=_new_id)
    run_id: str = ""
    conversation_id: str | None = None
    timestamp: datetime = field(default_factory=_now)
    #: Logical operation name, e.g. ``messages.SendMessage`` or ``sandbox.execute``.
    method: str = ""
    risk: str = ""
    decision: str = ""
    #: Peer/chat the operation targeted, when there is one.
    target: str | None = None
    #: Hash of the arguments. The arguments themselves are user data and are not
    #: stored by default; the digest still proves two calls were identical.
    argument_digest: str = ""
    #: Small, redacted argument summary when ``log_call_arguments`` is enabled.
    argument_preview: str | None = None
    succeeded: bool = True
    error: str | None = None
    duration_ms: float = 0.0
    #: Injection-scanner score for the content this call returned, 0.0 when clean.
    #: Separate from ``error`` on purpose: flagged content is not a failed call.
    suspicion: float = 0.0
    #: Where the call came from: ``tool``, ``sandbox``, or ``scheduler``.
    origin: str = "tool"
