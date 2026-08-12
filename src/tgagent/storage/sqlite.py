"""SQLite persistence.

One connection, guarded by an ``asyncio.Lock`` for writes. That matches SQLite's
actual concurrency model — a single writer — rather than pretending otherwise
and discovering it under load. WAL mode keeps reads from blocking on the writer.

Everything here implements the protocols in :mod:`tgagent.storage.base`; nothing
outside this module should import it directly except the composition root.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import aiosqlite

from tgagent.errors import MigrationError, StorageError
from tgagent.observability.logging import get_logger
from tgagent.storage.migrations import MIGRATIONS, SCHEMA_VERSION
from tgagent.storage.models import (
    AuditEntry,
    Conversation,
    MemoryFact,
    MessageRole,
    ScheduledTask,
    ScheduleKind,
    StoredMessage,
    TaskStatus,
)

log = get_logger(__name__)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_dt(value: str | None) -> datetime:
    parsed = _parse_dt(value)
    if parsed is None:
        raise StorageError("Expected a timestamp column to be non-null.")
    return parsed


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StorageError(f"Corrupt JSON column: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


class SQLiteStorage:
    """Concrete :class:`~tgagent.storage.base.Storage` backed by SQLite."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

        self.conversations = _ConversationRepo(self)
        self.memory = _MemoryRepo(self)
        self.tasks = _TaskRepo(self)
        self.audit = _AuditRepo(self)

    # ------------------------------------------------------------ lifecycle --
    async def connect(self) -> None:
        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = await aiosqlite.connect(self._path, isolation_level=None)
        except Exception as exc:
            raise StorageError(f"Cannot open database at {self._path}: {exc}") from exc

        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        await self._migrate()
        log.info("storage.connected", path=str(self._path), schema_version=SCHEMA_VERSION)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------ internals --
    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise StorageError("Storage is not connected. Call connect() first.")
        return self._db

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._write_lock:
            await self.db.execute(sql, params)

    async def execute_returning_rowcount(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self._write_lock:
            cursor = await self.db.execute(sql, params)
            return cursor.rowcount

    async def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cursor = await self.db.execute(sql, params)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def _migrate(self) -> None:
        row = await self.query_one("PRAGMA user_version")
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise MigrationError(
                f"Database schema version {current} is newer than this build supports "
                f"({SCHEMA_VERSION}). Upgrade tgagent or point at a different database."
            )
        for version, description, statements in MIGRATIONS:
            if version <= current:
                continue
            log.info("storage.migrating", version=version, description=description)
            async with self._write_lock:
                await self.db.execute("BEGIN")
                try:
                    for statement in statements:
                        await self.db.execute(statement)
                    await self.db.execute(f"PRAGMA user_version={version}")
                    await self.db.execute("COMMIT")
                except Exception as exc:
                    await self.db.execute("ROLLBACK")
                    raise MigrationError(
                        f"Migration {version} ({description}) failed: {exc}"
                    ) from exc


# ------------------------------------------------------------ conversations --
class _ConversationRepo:
    def __init__(self, store: SQLiteStorage) -> None:
        self._s = store

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        await self._s.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                conversation.id,
                conversation.title,
                _iso(conversation.created_at),
                _iso(conversation.updated_at),
                json.dumps(conversation.metadata),
            ),
        )
        return conversation

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = await self._s.query_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        return self._row_to_conversation(row) if row else None

    async def list_conversations(self, *, limit: int = 20, offset: int = 0) -> list[Conversation]:
        rows = await self._s.query(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [self._row_to_conversation(r) for r in rows]

    async def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None:
        if title is None:
            await self._s.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_iso(datetime.now(UTC)), conversation_id),
            )
        else:
            await self._s.execute(
                "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ?",
                (_iso(datetime.now(UTC)), title, conversation_id),
            )

    async def delete_conversation(self, conversation_id: str) -> bool:
        return (
            await self._s.execute_returning_rowcount(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            > 0
        )

    async def add_message(self, message: StoredMessage) -> StoredMessage:
        await self._s.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at, token_estimate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.conversation_id,
                message.role.value,
                json.dumps(message.content),
                _iso(message.created_at),
                message.token_estimate,
            ),
        )
        return message

    async def get_messages(
        self, conversation_id: str, *, limit: int | None = None
    ) -> list[StoredMessage]:
        # Ordering by rowid rather than created_at: two turns can share a
        # timestamp, and insertion order is the truth we care about.
        if limit is None:
            rows = await self._s.query(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY rowid",
                (conversation_id,),
            )
        else:
            # `rowid` must be projected explicitly: `SELECT *` omits it, so the
            # outer ORDER BY would have no such column to sort on.
            rows = await self._s.query(
                "SELECT * FROM (SELECT rowid AS _seq, * FROM messages "
                "WHERE conversation_id = ? ORDER BY rowid DESC LIMIT ?) ORDER BY _seq",
                (conversation_id, limit),
            )
        return [self._row_to_message(r) for r in rows]

    @staticmethod
    def _row_to_conversation(row: aiosqlite.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            title=row["title"],
            created_at=_require_dt(row["created_at"]),
            updated_at=_require_dt(row["updated_at"]),
            metadata=_json_loads(row["metadata"]),
        )

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> StoredMessage:
        return StoredMessage(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=MessageRole(row["role"]),
            content=_json_loads(row["content"]),
            created_at=_require_dt(row["created_at"]),
            token_estimate=int(row["token_estimate"]),
        )


# -------------------------------------------------------------------- memory --
class _MemoryRepo:
    def __init__(self, store: SQLiteStorage) -> None:
        self._s = store

    async def put(self, fact: MemoryFact) -> MemoryFact:
        fact.updated_at = datetime.now(UTC)
        await self._s.execute(
            "INSERT INTO memory_facts (id, key, value, category, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, category=excluded.category, "
            "source=excluded.source, updated_at=excluded.updated_at",
            (
                fact.id,
                fact.key,
                fact.value,
                fact.category,
                fact.source,
                _iso(fact.created_at),
                _iso(fact.updated_at),
            ),
        )
        return fact

    async def get(self, key: str) -> MemoryFact | None:
        row = await self._s.query_one("SELECT * FROM memory_facts WHERE key = ?", (key,))
        return self._row_to_fact(row) if row else None

    async def search(self, query: str, *, limit: int = 20) -> list[MemoryFact]:
        # LIKE is adequate at the scale this table reaches (hundreds of rows) and
        # avoids depending on the FTS5 module, which is not built into every
        # SQLite distribution.
        pattern = f"%{query.strip()}%"
        rows = await self._s.query(
            "SELECT * FROM memory_facts WHERE key LIKE ? OR value LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (pattern, pattern, limit),
        )
        return [self._row_to_fact(r) for r in rows]

    async def list_all(self, *, category: str | None = None, limit: int = 200) -> list[MemoryFact]:
        if category:
            rows = await self._s.query(
                "SELECT * FROM memory_facts WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = await self._s.query(
                "SELECT * FROM memory_facts ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return [self._row_to_fact(r) for r in rows]

    async def delete(self, key: str) -> bool:
        return (
            await self._s.execute_returning_rowcount(
                "DELETE FROM memory_facts WHERE key = ?", (key,)
            )
            > 0
        )

    @staticmethod
    def _row_to_fact(row: aiosqlite.Row) -> MemoryFact:
        return MemoryFact(
            id=row["id"],
            key=row["key"],
            value=row["value"],
            category=row["category"],
            source=row["source"],
            created_at=_require_dt(row["created_at"]),
            updated_at=_require_dt(row["updated_at"]),
        )


# --------------------------------------------------------------------- tasks --
class _TaskRepo:
    _COLUMNS = (
        "id, name, prompt, kind, expression, timezone, enabled, next_run_at, "
        "last_run_at, last_status, last_error, run_count, created_at, metadata"
    )

    def __init__(self, store: SQLiteStorage) -> None:
        self._s = store

    async def create(self, task: ScheduledTask) -> ScheduledTask:
        await self._s.execute(
            # _COLUMNS is a class constant, never user input.
            f"INSERT INTO scheduled_tasks ({self._COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._to_params(task),
        )
        return task

    async def get(self, task_id: str) -> ScheduledTask | None:
        row = await self._s.query_one("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return self._row_to_task(row) if row else None

    async def get_by_name(self, name: str) -> ScheduledTask | None:
        row = await self._s.query_one("SELECT * FROM scheduled_tasks WHERE name = ?", (name,))
        return self._row_to_task(row) if row else None

    async def list_all(self, *, enabled_only: bool = False) -> list[ScheduledTask]:
        sql = "SELECT * FROM scheduled_tasks"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return [self._row_to_task(r) for r in await self._s.query(sql)]

    async def due(self, now: datetime, *, limit: int = 10) -> list[ScheduledTask]:
        rows = await self._s.query(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at LIMIT ?",
            (_iso(now), limit),
        )
        return [self._row_to_task(r) for r in rows]

    async def update(self, task: ScheduledTask) -> ScheduledTask:
        await self._s.execute(
            "UPDATE scheduled_tasks SET name=?, prompt=?, kind=?, expression=?, timezone=?, "
            "enabled=?, next_run_at=?, last_run_at=?, last_status=?, last_error=?, run_count=?, "
            "metadata=? WHERE id=?",
            (
                task.name,
                task.prompt,
                task.kind.value,
                task.expression,
                task.timezone,
                int(task.enabled),
                _iso(task.next_run_at),
                _iso(task.last_run_at),
                task.last_status.value if task.last_status else None,
                task.last_error,
                task.run_count,
                json.dumps(task.metadata),
                task.id,
            ),
        )
        return task

    async def delete(self, task_id: str) -> bool:
        return (
            await self._s.execute_returning_rowcount(
                "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,)
            )
            > 0
        )

    async def claim(self, task_id: str, now: datetime) -> bool:
        """Compare-and-swap on ``next_run_at``.

        The ``next_run_at <= now`` guard in the UPDATE is what makes this safe:
        two schedulers racing on the same row will both issue the statement, but
        only the first changes a row, because the winner clears ``next_run_at``.
        """
        changed = await self._s.execute_returning_rowcount(
            "UPDATE scheduled_tasks SET last_run_at = ?, last_status = ?, next_run_at = NULL "
            "WHERE id = ? AND enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (_iso(now), TaskStatus.RUNNING.value, task_id, _iso(now)),
        )
        return changed > 0

    @staticmethod
    def _to_params(task: ScheduledTask) -> tuple[Any, ...]:
        return (
            task.id,
            task.name,
            task.prompt,
            task.kind.value,
            task.expression,
            task.timezone,
            int(task.enabled),
            _iso(task.next_run_at),
            _iso(task.last_run_at),
            task.last_status.value if task.last_status else None,
            task.last_error,
            task.run_count,
            _iso(task.created_at),
            json.dumps(task.metadata),
        )

    @staticmethod
    def _row_to_task(row: aiosqlite.Row) -> ScheduledTask:
        return ScheduledTask(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            kind=ScheduleKind(row["kind"]),
            expression=row["expression"],
            timezone=row["timezone"],
            enabled=bool(row["enabled"]),
            next_run_at=_parse_dt(row["next_run_at"]),
            last_run_at=_parse_dt(row["last_run_at"]),
            last_status=TaskStatus(row["last_status"]) if row["last_status"] else None,
            last_error=row["last_error"],
            run_count=int(row["run_count"]),
            created_at=_require_dt(row["created_at"]),
            metadata=_json_loads(row["metadata"]),
        )


# --------------------------------------------------------------------- audit --
class _AuditRepo:
    def __init__(self, store: SQLiteStorage) -> None:
        self._s = store

    async def record(self, entry: AuditEntry) -> None:
        await self._s.execute(
            "INSERT INTO audit_log (id, run_id, conversation_id, timestamp, method, risk, "
            "decision, target, argument_digest, argument_preview, succeeded, error, "
            "duration_ms, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.run_id,
                entry.conversation_id,
                _iso(entry.timestamp),
                entry.method,
                entry.risk,
                entry.decision,
                entry.target,
                entry.argument_digest,
                entry.argument_preview,
                int(entry.succeeded),
                entry.error,
                entry.duration_ms,
                entry.origin,
            ),
        )

    async def list_recent(self, *, run_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        if run_id:
            rows = await self._s.query(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY timestamp DESC LIMIT ?",
                (run_id, limit),
            )
        else:
            rows = await self._s.query(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
        return [self._row_to_entry(r) for r in rows]

    async def prune(self, older_than: datetime) -> int:
        return await self._s.execute_returning_rowcount(
            "DELETE FROM audit_log WHERE timestamp < ?", (_iso(older_than),)
        )

    @staticmethod
    def _row_to_entry(row: aiosqlite.Row) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            timestamp=_require_dt(row["timestamp"]),
            method=row["method"],
            risk=row["risk"],
            decision=row["decision"],
            target=row["target"],
            argument_digest=row["argument_digest"],
            argument_preview=row["argument_preview"],
            succeeded=bool(row["succeeded"]),
            error=row["error"],
            duration_ms=float(row["duration_ms"]),
            origin=row["origin"],
        )
