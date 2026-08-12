"""Scheduling: trigger arithmetic, the loop, claiming, and misfire handling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tgagent.config.settings import SchedulerSettings
from tgagent.errors import SchedulerError
from tgagent.scheduler.scheduler import Scheduler
from tgagent.scheduler.triggers import describe_schedule, next_run_after, validate_schedule
from tgagent.storage.models import ScheduledTask, ScheduleKind, TaskStatus
from tgagent.storage.sqlite import SQLiteStorage


class TestTriggers:
    def test_valid_cron_accepted(self) -> None:
        for expression in ("0 8 * * *", "*/15 * * * *", "0 0 1 * *", "30 6 * * 1-5"):
            validate_schedule(ScheduleKind.CRON, expression)

    def test_invalid_cron_explains_the_format(self) -> None:
        with pytest.raises(SchedulerError, match="5 fields"):
            validate_schedule(ScheduleKind.CRON, "not a cron")

    def test_unknown_timezone_rejected(self) -> None:
        with pytest.raises(SchedulerError, match="Unknown timezone"):
            validate_schedule(ScheduleKind.CRON, "0 8 * * *", "Mars/Olympus_Mons")

    def test_interval_floor_prevents_hot_loops(self) -> None:
        with pytest.raises(SchedulerError, match="minimum interval"):
            validate_schedule(ScheduleKind.INTERVAL, "1")

    def test_interval_must_be_numeric(self) -> None:
        with pytest.raises(SchedulerError, match="number of seconds"):
            validate_schedule(ScheduleKind.INTERVAL, "hourly")

    def test_once_requires_a_timestamp(self) -> None:
        validate_schedule(ScheduleKind.ONCE, "2026-09-01T08:00:00Z")
        with pytest.raises(SchedulerError, match="ISO-8601"):
            validate_schedule(ScheduleKind.ONCE, "next tuesday")

    def test_cron_next_run_is_in_the_future(self) -> None:
        now = datetime(2026, 3, 1, 7, 30, tzinfo=UTC)
        nxt = next_run_after(ScheduleKind.CRON, "0 8 * * *", "UTC", now)
        assert nxt == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)

    def test_cron_respects_the_timezone(self) -> None:
        now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        london = next_run_after(ScheduleKind.CRON, "0 8 * * *", "Europe/London", now)
        utc = next_run_after(ScheduleKind.CRON, "0 8 * * *", "UTC", now)
        # British Summer Time puts local 08:00 at 07:00 UTC.
        assert london != utc
        assert london.astimezone(UTC).hour == 7

    def test_interval_next_run(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert next_run_after(ScheduleKind.INTERVAL, "3600", "UTC", now) == now + timedelta(hours=1)

    def test_once_returns_none_when_already_past(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=UTC)
        assert next_run_after(ScheduleKind.ONCE, "2020-01-01T00:00:00Z", "UTC", now) is None

    def test_describe_is_readable(self) -> None:
        assert "cron" in describe_schedule(
            ScheduledTask(kind=ScheduleKind.CRON, expression="0 8 * * *")
        )
        assert (
            describe_schedule(ScheduledTask(kind=ScheduleKind.INTERVAL, expression="3600"))
            == "every 1h"
        )
        assert (
            describe_schedule(ScheduledTask(kind=ScheduleKind.INTERVAL, expression="300"))
            == "every 5m"
        )


class TestSchedulerLoop:
    async def test_due_task_runs(self, storage: SQLiteStorage) -> None:
        ran: list[str] = []

        async def runner(task: ScheduledTask) -> str:
            ran.append(task.name)
            return "done"

        now = datetime.now(UTC)
        await storage.tasks.create(
            ScheduledTask(
                name="due-now",
                prompt="review",
                kind=ScheduleKind.INTERVAL,
                expression="60",
                next_run_at=now - timedelta(seconds=5),
            )
        )
        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings(tick_interval=0.05))
        started = await scheduler.tick()
        await scheduler.stop()

        assert started == 1
        assert ran == ["due-now"]

    async def test_future_task_does_not_run(self, storage: SQLiteStorage) -> None:
        ran: list[str] = []

        async def runner(task: ScheduledTask) -> str:
            ran.append(task.name)
            return ""

        await storage.tasks.create(
            ScheduledTask(
                name="later",
                kind=ScheduleKind.INTERVAL,
                expression="60",
                next_run_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings())
        assert await scheduler.tick() == 0
        assert ran == []

    async def test_next_run_is_recomputed_after_success(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            return "ok"

        now = datetime.now(UTC)
        task = ScheduledTask(
            name="recurring",
            kind=ScheduleKind.INTERVAL,
            expression="60",
            next_run_at=now - timedelta(seconds=1),
        )
        await storage.tasks.create(task)

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings())
        await scheduler.tick()
        await asyncio.sleep(0.1)
        await scheduler.stop()

        reloaded = await storage.tasks.get(task.id)
        assert reloaded.last_status is TaskStatus.SUCCEEDED
        assert reloaded.run_count == 1
        assert reloaded.next_run_at is not None
        assert reloaded.next_run_at > now

    async def test_a_failing_task_is_recorded_and_rescheduled(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            raise RuntimeError("the task blew up")

        task = ScheduledTask(
            name="flaky",
            kind=ScheduleKind.INTERVAL,
            expression="60",
            next_run_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await storage.tasks.create(task)

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings())
        await scheduler.tick()
        await asyncio.sleep(0.1)
        await scheduler.stop()

        reloaded = await storage.tasks.get(task.id)
        assert reloaded.last_status is TaskStatus.FAILED
        assert "blew up" in (reloaded.last_error or "")
        # A failure must not stop the schedule.
        assert reloaded.next_run_at is not None

    async def test_one_shot_tasks_disable_themselves(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            return "ok"

        task = ScheduledTask(
            name="one-shot",
            kind=ScheduleKind.ONCE,
            expression="2026-01-01T00:00:00Z",
            next_run_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        await storage.tasks.create(task)

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings())
        await scheduler.tick()
        await asyncio.sleep(0.1)
        await scheduler.stop()

        reloaded = await storage.tasks.get(task.id)
        assert not reloaded.enabled
        assert reloaded.next_run_at is None

    async def test_badly_overdue_tasks_are_skipped_not_stampeded(
        self, storage: SQLiteStorage
    ) -> None:
        # A laptop waking after a weekend must not fire three days of dailies.
        ran: list[str] = []

        async def runner(task: ScheduledTask) -> str:
            ran.append(task.name)
            return ""

        task = ScheduledTask(
            name="stale",
            kind=ScheduleKind.INTERVAL,
            expression="60",
            next_run_at=datetime.now(UTC) - timedelta(days=3),
        )
        await storage.tasks.create(task)

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings(misfire_grace=60.0))
        await scheduler.tick()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert ran == []
        reloaded = await storage.tasks.get(task.id)
        assert reloaded.last_status is TaskStatus.SKIPPED
        assert reloaded.next_run_at is not None  # rescheduled, not stranded

    async def test_claiming_prevents_double_execution(self, storage: SQLiteStorage) -> None:
        runs = 0

        async def runner(_task: ScheduledTask) -> str:
            nonlocal runs
            runs += 1
            return ""

        await storage.tasks.create(
            ScheduledTask(
                name="contended",
                kind=ScheduleKind.INTERVAL,
                expression="60",
                next_run_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        settings = SchedulerSettings()
        a = Scheduler(storage.tasks, runner, settings)
        b = Scheduler(storage.tasks, runner, settings)

        await asyncio.gather(a.tick(), b.tick())
        await asyncio.sleep(0.1)
        await asyncio.gather(a.stop(), b.stop())

        assert runs == 1

    async def test_reconcile_repairs_a_missing_next_run(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            return ""

        task = ScheduledTask(
            name="orphaned",
            kind=ScheduleKind.CRON,
            expression="0 8 * * *",
            next_run_at=None,  # e.g. the process died mid-run
        )
        await storage.tasks.create(task)

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings())
        assert await scheduler.reconcile() == 1
        assert (await storage.tasks.get(task.id)).next_run_at is not None

    async def test_disabled_scheduler_does_not_start(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            return ""

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings(enabled=False))
        await scheduler.start()
        assert scheduler.active == 0
        await scheduler.stop()

    async def test_start_stop_is_clean(self, storage: SQLiteStorage) -> None:
        async def runner(_task: ScheduledTask) -> str:
            return ""

        scheduler = Scheduler(storage.tasks, runner, SchedulerSettings(tick_interval=0.05))
        await scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()
        assert scheduler.active == 0
