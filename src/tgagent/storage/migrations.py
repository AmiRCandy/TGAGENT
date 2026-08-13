"""Versioned schema migrations.

Explicit, ordered, forward-only SQL. Each migration is a list of statements run
in a single transaction; ``PRAGMA user_version`` records how far the database
has come. Adding a migration means appending to :data:`MIGRATIONS` — never
editing an existing entry, because deployed databases have already run it.
"""

from __future__ import annotations

from typing import Final

Migration = tuple[int, str, tuple[str, ...]]

_V1: Final = (
    """
    CREATE TABLE conversations (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        metadata    TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC)",
    """
    CREATE TABLE messages (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL,
        content         TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        token_estimate  INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Indexed on conversation_id alone: SQLite stores the rowid as the index
    # key, so entries are already in insertion order within each conversation —
    # which is exactly the order get_messages() reads them in. (`rowid` cannot
    # appear in an index expression.)
    "CREATE INDEX idx_messages_conversation ON messages(conversation_id)",
    """
    CREATE TABLE memory_facts (
        id          TEXT PRIMARY KEY,
        key         TEXT NOT NULL UNIQUE,
        value       TEXT NOT NULL,
        category    TEXT NOT NULL DEFAULT 'general',
        source      TEXT NOT NULL DEFAULT 'agent',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_memory_category ON memory_facts(category)",
    """
    CREATE TABLE scheduled_tasks (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL UNIQUE,
        prompt       TEXT NOT NULL,
        kind         TEXT NOT NULL,
        expression   TEXT NOT NULL,
        timezone     TEXT NOT NULL DEFAULT 'UTC',
        enabled      INTEGER NOT NULL DEFAULT 1,
        next_run_at  TEXT,
        last_run_at  TEXT,
        last_status  TEXT,
        last_error   TEXT,
        run_count    INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL,
        metadata     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_tasks_due ON scheduled_tasks(enabled, next_run_at)",
    """
    CREATE TABLE audit_log (
        id               TEXT PRIMARY KEY,
        run_id           TEXT NOT NULL,
        conversation_id  TEXT,
        timestamp        TEXT NOT NULL,
        method           TEXT NOT NULL,
        risk             TEXT NOT NULL,
        decision         TEXT NOT NULL,
        target           TEXT,
        argument_digest  TEXT NOT NULL DEFAULT '',
        argument_preview TEXT,
        succeeded        INTEGER NOT NULL DEFAULT 1,
        error            TEXT,
        duration_ms      REAL NOT NULL DEFAULT 0,
        origin           TEXT NOT NULL DEFAULT 'tool'
    )
    """,
    "CREATE INDEX idx_audit_run ON audit_log(run_id, timestamp)",
    "CREATE INDEX idx_audit_time ON audit_log(timestamp DESC)",
)

# The injection scanner's score for content a call returned. It used to be
# appended to `error`, which made every successful read of flagged content look
# like a failed call — and lost the number as data. It is a property of the
# content, not a failure, so it gets its own column.
_V2: Final = ("ALTER TABLE audit_log ADD COLUMN suspicion REAL NOT NULL DEFAULT 0",)

# Standing instructions to answer a chat on the owner's behalf. One per chat,
# enforced here rather than in code: two watches on the same chat would both fire
# on the same message, and "which one won" is not a question worth having.
_V3: Final = (
    """
    CREATE TABLE chat_watches (
        id              TEXT PRIMARY KEY,
        chat_id         INTEGER NOT NULL UNIQUE,
        chat_title      TEXT NOT NULL DEFAULT '',
        instruction     TEXT NOT NULL,
        senders         TEXT NOT NULL DEFAULT '[]',
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        expires_at      TEXT,
        max_replies     INTEGER NOT NULL DEFAULT 20,
        reply_count     INTEGER NOT NULL DEFAULT 0,
        last_reply_at   TEXT,
        stopped_because TEXT,
        metadata        TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX idx_watches_enabled ON chat_watches(enabled)",
)

#: ``(version, description, statements)``, applied in ascending order.
MIGRATIONS: Final[tuple[Migration, ...]] = (
    (1, "initial schema", _V1),
    (2, "record content suspicion on audit entries", _V2),
    (3, "chat watches for automatic replies", _V3),
)

SCHEMA_VERSION: Final[int] = max(m[0] for m in MIGRATIONS)
