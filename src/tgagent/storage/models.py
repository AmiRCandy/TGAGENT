"""Records that cross the persistence boundary.

Plain dataclasses rather than ORM entities: the storage layer is deliberately
thin, and keeping the records free of database machinery is what makes the
repository protocols swappable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class MessageRole(str, Enum):
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


class ScheduleKind(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"


class TaskStatus(str, Enum):
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
    #: Where the call came from: ``tool``, ``sandbox``, or ``scheduler``.
    origin: str = "tool"
