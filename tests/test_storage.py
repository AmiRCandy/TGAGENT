"""Persistence: migrations, repositories, and the concurrency guarantees."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgagent.errors import MigrationError, StorageError
from tgagent.storage.migrations import SCHEMA_VERSION
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
from tgagent.storage.sqlite import SQLiteStorage


class TestLifecycle:
    async def test_migrations_run_and_are_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite"
        async with SQLiteStorage(path) as store:
            row = await store.query_one("PRAGMA user_version")
            assert int(row[0]) == SCHEMA_VERSION

        # Re-opening must not re-run migrations.
        async with SQLiteStorage(path) as store:
            row = await store.query_one("PRAGMA user_version")
            assert int(row[0]) == SCHEMA_VERSION

    async def test_newer_schema_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite"
        async with SQLiteStorage(path) as store:
            await store.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
        with pytest.raises(MigrationError, match="newer than this build"):
            async with SQLiteStorage(path):
                pass

    async def test_a_refused_migration_leaves_no_open_connection(self, tmp_path: Path) -> None:
        """A failed connect() must not leak the connection it had already opened.

        aiosqlite runs each connection on its own *non-daemon* thread, so a leaked
        one keeps the interpreter alive at shutdown: `tgagent` would print the
        migration error and then hang forever instead of exiting. `__aexit__` is
        no help either, because `__aenter__` is what raised.
        """
        path = tmp_path / "db.sqlite"
        async with SQLiteStorage(path) as store:
            await store.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")

        threads_before = threading.active_count()
        store = SQLiteStorage(path)
        with pytest.raises(MigrationError):
            await store.connect()

        assert store._db is None
        with pytest.raises(StorageError, match="not connected"):
            _ = store.db
        # Give the connection thread a moment to notice it was closed.
        for _ in range(50):
            if threading.active_count() <= threads_before:
                break
            await asyncio.sleep(0.01)
        assert threading.active_count() <= threads_before

    async def test_using_before_connect_is_a_clear_error(self, tmp_path: Path) -> None:
        store = SQLiteStorage(tmp_path / "db.sqlite")
        with pytest.raises(StorageError, match="not connected"):
            _ = store.db

    async def test_foreign_keys_are_enforced(self, storage: SQLiteStorage) -> None:
        row = await storage.query_one("PRAGMA foreign_keys")
        assert int(row[0]) == 1


class TestConversations:
    async def test_round_trip(self, storage: SQLiteStorage) -> None:
        conversation = await storage.conversations.create_conversation(
            Conversation(title="Summarise January")
        )
        fetched = await storage.conversations.get_conversation(conversation.id)
        assert fetched is not None
        assert fetched.title == "Summarise January"

    async def test_messages_come_back_in_insertion_order(self, storage: SQLiteStorage) -> None:
        conversation = await storage.conversations.create_conversation(Conversation())
        # Identical timestamps: ordering must come from insertion, not the clock.
        stamp = datetime.now(UTC)
        for i in range(10):
            await storage.conversations.add_message(
                StoredMessage(
                    conversation_id=conversation.id,
                    role=MessageRole.USER,
                    content={"role": "user", "content": [{"type": "text", "text": str(i)}]},
                    created_at=stamp,
                )
            )
        messages = await storage.conversations.get_messages(conversation.id)
        assert [m.content["content"][0]["text"] for m in messages] == [str(i) for i in range(10)]

    async def test_limit_keeps_the_most_recent_but_preserves_order(
        self, storage: SQLiteStorage
    ) -> None:
        conversation = await storage.conversations.create_conversation(Conversation())
        for i in range(20):
            await storage.conversations.add_message(
                StoredMessage(
                    conversation_id=conversation.id,
                    role=MessageRole.USER,
                    content={"role": "user", "content": [{"type": "text", "text": str(i)}]},
                )
            )
        recent = await storage.conversations.get_messages(conversation.id, limit=5)
        assert [m.content["content"][0]["text"] for m in recent] == ["15", "16", "17", "18", "19"]

    async def test_deleting_a_conversation_cascades_to_messages(
        self, storage: SQLiteStorage
    ) -> None:
        conversation = await storage.conversations.create_conversation(Conversation())
        await storage.conversations.add_message(
            StoredMessage(conversation_id=conversation.id, content={"role": "user", "content": []})
        )
        assert await storage.conversations.delete_conversation(conversation.id)
        assert await storage.conversations.get_messages(conversation.id) == []

    async def test_missing_conversation_returns_none(self, storage: SQLiteStorage) -> None:
        assert await storage.conversations.get_conversation("nope") is None


class TestMemory:
    async def test_put_is_an_upsert_keyed_on_key(self, storage: SQLiteStorage) -> None:
        await storage.memory.put(MemoryFact(key="user.timezone", value="UTC"))
        await storage.memory.put(MemoryFact(key="user.timezone", value="Europe/London"))
        fact = await storage.memory.get("user.timezone")
        assert fact is not None
        assert fact.value == "Europe/London"
        assert len(await storage.memory.list_all()) == 1

    async def test_search_matches_keys_and_values(self, storage: SQLiteStorage) -> None:
        await storage.memory.put(MemoryFact(key="project.alpha", value="Alex leads it"))
        await storage.memory.put(MemoryFact(key="project.beta", value="John leads it"))
        assert len(await storage.memory.search("alpha")) == 1
        assert len(await storage.memory.search("leads it")) == 2
        assert await storage.memory.search("nothing at all") == []

    async def test_category_filter(self, storage: SQLiteStorage) -> None:
        await storage.memory.put(MemoryFact(key="a", value="1", category="preference"))
        await storage.memory.put(MemoryFact(key="b", value="2", category="project"))
        assert len(await storage.memory.list_all(category="preference")) == 1

    async def test_delete(self, storage: SQLiteStorage) -> None:
        await storage.memory.put(MemoryFact(key="temp", value="x"))
        assert await storage.memory.delete("temp")
        assert not await storage.memory.delete("temp")


class TestTasks:
    async def test_round_trip_and_lookup_by_name(self, storage: SQLiteStorage) -> None:
        task = ScheduledTask(name="daily", prompt="review unread", expression="0 8 * * *")
        await storage.tasks.create(task)
        assert (await storage.tasks.get_by_name("daily")).id == task.id
        assert (await storage.tasks.get(task.id)).name == "daily"

    async def test_due_returns_only_enabled_and_overdue(self, storage: SQLiteStorage) -> None:
        now = datetime.now(UTC)
        await storage.tasks.create(
            ScheduledTask(
                name="past", expression="* * * * *", next_run_at=now - timedelta(minutes=1)
            )
        )
        await storage.tasks.create(
            ScheduledTask(
                name="future", expression="* * * * *", next_run_at=now + timedelta(hours=1)
            )
        )
        await storage.tasks.create(
            ScheduledTask(
                name="disabled",
                expression="* * * * *",
                enabled=False,
                next_run_at=now - timedelta(minutes=1),
            )
        )
        due = await storage.tasks.due(now)
        assert [t.name for t in due] == ["past"]

    async def test_claim_is_a_compare_and_swap(self, storage: SQLiteStorage) -> None:
        now = datetime.now(UTC)
        task = ScheduledTask(
            name="race", expression="* * * * *", next_run_at=now - timedelta(seconds=1)
        )
        await storage.tasks.create(task)

        # Only one of several concurrent claimants may win.
        results = await asyncio.gather(*(storage.tasks.claim(task.id, now) for _ in range(5)))
        assert sum(results) == 1

    async def test_claim_fails_for_a_disabled_task(self, storage: SQLiteStorage) -> None:
        now = datetime.now(UTC)
        task = ScheduledTask(
            name="off",
            expression="* * * * *",
            enabled=False,
            next_run_at=now - timedelta(seconds=1),
        )
        await storage.tasks.create(task)
        assert not await storage.tasks.claim(task.id, now)

    async def test_update_persists_status_and_metadata(self, storage: SQLiteStorage) -> None:
        task = ScheduledTask(name="t", expression="0 * * * *", metadata={"created_by": "cli"})
        await storage.tasks.create(task)
        task.last_status = TaskStatus.FAILED
        task.last_error = "boom"
        task.run_count = 3
        task.kind = ScheduleKind.INTERVAL
        await storage.tasks.update(task)

        reloaded = await storage.tasks.get(task.id)
        assert reloaded.last_status is TaskStatus.FAILED
        assert reloaded.last_error == "boom"
        assert reloaded.run_count == 3
        assert reloaded.kind is ScheduleKind.INTERVAL
        assert reloaded.metadata == {"created_by": "cli"}


class TestAudit:
    async def test_record_and_filter_by_run(self, storage: SQLiteStorage) -> None:
        for run in ("a", "a", "b"):
            await storage.audit.record(
                AuditEntry(run_id=run, method="get_messages", risk="read_only", decision="allow")
            )
        assert len(await storage.audit.list_recent(run_id="a")) == 2
        assert len(await storage.audit.list_recent()) == 3

    async def test_prune_removes_only_old_entries(self, storage: SQLiteStorage) -> None:
        now = datetime.now(UTC)
        await storage.audit.record(AuditEntry(run_id="old", timestamp=now - timedelta(days=100)))
        await storage.audit.record(AuditEntry(run_id="new", timestamp=now))
        removed = await storage.audit.prune(now - timedelta(days=30))
        assert removed == 1
        assert [e.run_id for e in await storage.audit.list_recent()] == ["new"]


class TestAuditSuspicion:
    """The scanner's score is part of the record, not part of the error text.

    It used to be appended to `error`, which made a *successful* read of flagged
    content indistinguishable from a failed call to anything querying the log —
    and lost the number as data.
    """

    async def test_the_score_round_trips(self, storage: SQLiteStorage) -> None:
        await storage.audit.record(
            AuditEntry(method="get_messages", risk="read_only", decision="allow", suspicion=0.72)
        )
        entry = (await storage.audit.list_recent(limit=1))[0]
        assert entry.suspicion == 0.72
        assert entry.succeeded is True
        assert entry.error is None

    async def test_a_clean_call_scores_zero_rather_than_null(self, storage: SQLiteStorage) -> None:
        await storage.audit.record(AuditEntry(method="get_dialogs", decision="allow"))
        assert (await storage.audit.list_recent(limit=1))[0].suspicion == 0.0

    async def test_an_existing_database_gains_the_column(self, tmp_path: Path) -> None:
        """Migration 2 is an ALTER on a table deployed databases already have."""
        path = tmp_path / "db.sqlite"
        async with SQLiteStorage(path) as store:
            columns = {r["name"] for r in await store.query("PRAGMA table_info(audit_log)")}
            assert "suspicion" in columns

        # Re-opening must not try to add it twice.
        async with SQLiteStorage(path) as store:
            row = await store.query_one("PRAGMA user_version")
            assert int(row[0]) == SCHEMA_VERSION
