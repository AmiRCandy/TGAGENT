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

#: ``(version, description, statements)``, applied in ascending order.
MIGRATIONS: Final[tuple[Migration, ...]] = ((1, "initial schema", _V1),)

SCHEMA_VERSION: Final[int] = max(m[0] for m in MIGRATIONS)
