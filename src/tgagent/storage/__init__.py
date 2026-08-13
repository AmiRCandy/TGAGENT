"""Persistence: repository protocols, records, and the SQLite implementation."""

from tgagent.storage.base import (
    AuditRepository,
    ConversationRepository,
    MemoryRepository,
    Storage,
    TaskRepository,
    WatchRepository,
)
from tgagent.storage.models import (
    AuditEntry,
    ChatWatch,
    Conversation,
    MemoryFact,
    MessageRole,
    ScheduledTask,
    ScheduleKind,
    StoredMessage,
    TaskStatus,
)
from tgagent.storage.sqlite import SQLiteStorage

__all__ = [
    "AuditEntry",
    "AuditRepository",
    "ChatWatch",
    "Conversation",
    "ConversationRepository",
    "MemoryFact",
    "MemoryRepository",
    "MessageRole",
    "SQLiteStorage",
    "ScheduleKind",
    "ScheduledTask",
    "Storage",
    "StoredMessage",
    "TaskRepository",
    "TaskStatus",
    "WatchRepository",
]
