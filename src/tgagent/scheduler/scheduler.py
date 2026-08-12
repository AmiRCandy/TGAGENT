"""The task scheduler.

A single asyncio loop that wakes on a tick, asks storage which tasks are due,
claims each one atomically, and runs it through the agent. Deliberately built
here rather than pulled in:

* **Tasks are data, not pickled callables.** APScheduler's persistent job stores
  serialise Python objects, which breaks across upgrades and is a deserialisation
  risk. A row here is an id, a cron string, and a prompt — inspectable with
  ``sqlite3``, safe to restore into a different build.
* **Claiming is a database compare-and-swap**, so two processes pointed at the
  same database cannot double-fire a task.
* **Misfires are explicit.** A run more than ``misfire_grace`` seconds late is
  skipped and rescheduled rather than fired, so a laptop waking after a weekend
  does not run seven "daily summaries" back to back.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from tgagent.config.settings import SchedulerSettings
from tgagent.observability.logging import get_logger
from tgagent.scheduler.triggers import next_run_after
from tgagent.storage.base import TaskRepository
from tgagent.storage.models import ScheduledTask, ScheduleKind, TaskStatus

log = get_logger(__name__)

#: ``async (task) -> summary text``. Supplied by the composition root so the
#: scheduler has no dependency on the agent runtime.
TaskRunner = Callable[[ScheduledTask], Awaitable[str]]


class Scheduler:
    """Runs due tasks, one asyncio task per execution, bounded by a semaphore."""

    def __init__(
        self,
        tasks: TaskRepository,
        runner: TaskRunner,
        settings: SchedulerSettings,
    ) -> None:
        self._tasks = tasks
        self._runner = runner
        self._settings = settings
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)

    @property
    def active(self) -> int:
        return len(self._running)

    # ------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        if not self._settings.enabled:
            log.info("scheduler.disabled")
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping.clear()
        await self.reconcile()
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("scheduler.started", tick_interval=self._settings.tick_interval)

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop ticking and, by default, let in-flight tasks finish."""
        self._stopping.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

        if self._running:
            if drain:
                log.info("scheduler.draining", active=len(self._running))
                done, pending = await asyncio.wait(self._running, timeout=timeout)
                for task in pending:
                    task.cancel()
            else:
                for task in self._running:
                    task.cancel()
            await asyncio.gather(*self._running, return_exceptions=True)

        log.info("scheduler.stopped")

    async def reconcile(self) -> int:
        """Give every enabled task a ``next_run_at``.

        Called at start-up so a task whose row was written while the scheduler
        was down (or whose process died mid-run, leaving ``next_run_at`` null)
        is picked back up rather than stranded.
        """
        now = datetime.now(UTC)
        repaired = 0
        for task in await self._tasks.list_all(enabled_only=True):
            if task.next_run_at is not None:
                continue
            if task.kind is ScheduleKind.ONCE and task.run_count > 0:
                continue
            task.next_run_at = next_run_after(
                task.kind, task.expression, task.timezone, now
            )
            if task.next_run_at is None:
                task.enabled = False
            await self._tasks.update(task)
            repaired += 1
        if repaired:
            log.info("scheduler.reconciled", tasks=repaired)
        return repaired

    # ----------------------------------------------------------------- loop --
    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.error("scheduler.tick_failed", error=str(exc), exc_info=True)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.tick_interval
                )

    async def tick(self) -> int:
        """Fire everything currently due. Returns how many were started."""
        now = datetime.now(UTC)
        due = await self._tasks.due(now, limit=self._settings.max_concurrent_tasks * 2)
        started = 0

        for task in due:
            if self._stopping.is_set():
                break
            if not await self._tasks.claim(task.id, now):
                # Another scheduler took it, or it was disabled between the
                # query and the claim.
                continue

            if self._is_misfire(task, now):
                log.warning(
                    "scheduler.misfire_skipped",
                    task=task.name,
                    was_due=task.next_run_at.isoformat() if task.next_run_at else None,
                )
                await self._finish(task, TaskStatus.SKIPPED, error="Missed its window.")
                continue

            runner = asyncio.create_task(self._execute(task), name=f"task:{task.name}")
            self._running.add(runner)
            runner.add_done_callback(self._running.discard)
            started += 1

        return started

    def _is_misfire(self, task: ScheduledTask, now: datetime) -> bool:
        grace = self._settings.misfire_grace
        if grace <= 0 or task.next_run_at is None:
            return False
        return (now - task.next_run_at).total_seconds() > grace

    async def _execute(self, task: ScheduledTask) -> None:
        async with self._semaphore:
            log.info("scheduler.task_started", task=task.name)
            status = TaskStatus.SUCCEEDED
            error: str | None = None
            try:
                summary = await self._runner(task)
                log.info("scheduler.task_finished", task=task.name, summary=summary[:200])
            except asyncio.CancelledError:
                status, error = TaskStatus.FAILED, "Cancelled during shutdown."
                raise
            except Exception as exc:  # noqa: BLE001 - recorded, never propagated
                status = TaskStatus.FAILED
                error = f"{type(exc).__name__}: {exc}"
                log.error("scheduler.task_failed", task=task.name, error=error)
            finally:
                with contextlib.suppress(Exception):
                    await self._finish(task, status, error=error)

    async def _finish(
        self, task: ScheduledTask, status: TaskStatus, *, error: str | None
    ) -> None:
        """Record the outcome and compute the next fire time."""
        now = datetime.now(UTC)
        # Re-read: the row may have been edited (disabled, rescheduled) while
        # the task was running, and that edit should win.
        current = await self._tasks.get(task.id) or task
        current.last_run_at = now
        current.last_status = status
        current.last_error = error
        if status is not TaskStatus.SKIPPED:
            current.run_count += 1

        if current.kind is ScheduleKind.ONCE:
            current.enabled = False
            current.next_run_at = None
        elif current.enabled:
            current.next_run_at = next_run_after(
                current.kind, current.expression, current.timezone, now
            )

        await self._tasks.update(current)
