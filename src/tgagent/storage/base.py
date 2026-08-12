"""Repository protocols.

The rest of the project depends on these interfaces, never on SQLite. They are
narrow on purpose: every method here is one an existing caller needs, so a
Postgres or in-memory implementation is a bounded amount of work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgagent.storage.models import (
    AuditEntry,
    Conversation,
    MemoryFact,
    ScheduledTask,
    StoredMessage,
)


@runtime_checkable
class ConversationRepository(Protocol):
    """Agent conversations and their turns."""

    async def create_conversation(self, conversation: Conversation) -> Conversation: ...

    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    async def list_conversations(self, *, limit: int = 20, offset: int = 0) -> list[Conversation]: ...

    async def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None: ...

    async def delete_conversation(self, conversation_id: str) -> bool: ...

    async def add_message(self, message: StoredMessage) -> StoredMessage: ...

    async def get_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[StoredMessage]:
        """Return turns oldest-first. ``limit`` keeps the *most recent* N."""
        ...


@runtime_checkable
class MemoryRepository(Protocol):
    """Long-lived facts and preferences."""

    async def put(self, fact: MemoryFact) -> MemoryFact:
        """Insert, or update the existing fact with the same ``key``."""
        ...

    async def get(self, key: str) -> MemoryFact | None: ...

    async def search(self, query: str, *, limit: int = 20) -> list[MemoryFact]: ...

    async def list_all(self, *, category: str | None = None, limit: int = 200) -> list[MemoryFact]: ...

    async def delete(self, key: str) -> bool: ...


@runtime_checkable
class TaskRepository(Protocol):
    """Scheduled agent runs."""

    async def create(self, task: ScheduledTask) -> ScheduledTask: ...

    async def get(self, task_id: str) -> ScheduledTask | None: ...

    async def get_by_name(self, name: str) -> ScheduledTask | None: ...

    async def list_all(self, *, enabled_only: bool = False) -> list[ScheduledTask]: ...

    async def due(self, now: datetime, *, limit: int = 10) -> list[ScheduledTask]:
        """Enabled tasks whose ``next_run_at`` has passed."""
        ...

    async def update(self, task: ScheduledTask) -> ScheduledTask: ...

    async def delete(self, task_id: str) -> bool: ...

    async def claim(self, task_id: str, now: datetime) -> bool:
        """Atomically mark a task as running. False if someone else claimed it."""
        ...


@runtime_checkable
class AuditRepository(Protocol):
    """The security audit trail."""

    async def record(self, entry: AuditEntry) -> None: ...

    async def list_recent(
        self, *, run_id: str | None = None, limit: int = 100
    ) -> list[AuditEntry]: ...

    async def prune(self, older_than: datetime) -> int: ...


@runtime_checkable
class Storage(Protocol):
    """The composed persistence surface handed to the composition root."""

    conversations: ConversationRepository
    memory: MemoryRepository
    tasks: TaskRepository
    audit: AuditRepository

    async def connect(self) -> None: ...

    async def close(self) -> None: ...
