"""Schedule expressions: validation and next-fire computation.

Three kinds, all stored as a plain string so a task row stays inspectable with
``sqlite3``:

* ``cron`` — a 5-field expression evaluated in an IANA timezone. Timezone-aware
  because "every morning at 8" means local 8, and must keep meaning that across
  a DST transition.
* ``interval`` — a number of seconds.
* ``once`` — an ISO-8601 timestamp; the task disables itself after firing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter

from tgagent.errors import SchedulerError
from tgagent.storage.models import ScheduledTask, ScheduleKind

#: Guard against a typo turning into a hot loop.
MIN_INTERVAL_SECONDS = 30


def validate_schedule(kind: ScheduleKind, expression: str, timezone: str = "UTC") -> None:
    """Raise :class:`SchedulerError` with an actionable message if invalid."""
    expression = expression.strip()

    if kind is ScheduleKind.CRON:
        _resolve_timezone(timezone)
        if not croniter.is_valid(expression):
            raise SchedulerError(
                f"{expression!r} is not a valid cron expression. Use 5 fields: "
                f"minute hour day-of-month month day-of-week — e.g. '0 8 * * *' for "
                f"08:00 every day, or '*/15 * * * *' every fifteen minutes."
            )
        return

    if kind is ScheduleKind.INTERVAL:
        try:
            seconds = float(expression)
        except ValueError as exc:
            raise SchedulerError(
                f"An interval schedule needs a number of seconds, not {expression!r}."
            ) from exc
        if seconds < MIN_INTERVAL_SECONDS:
            raise SchedulerError(
                f"The minimum interval is {MIN_INTERVAL_SECONDS} seconds; {seconds:g} "
                f"would run continuously."
            )
        return

    if kind is ScheduleKind.ONCE:
        _parse_timestamp(expression)
        return

    raise SchedulerError(f"Unknown schedule kind {kind!r}.")


def next_run_after(
    kind: ScheduleKind, expression: str, timezone: str, after: datetime
) -> datetime | None:
    """The next fire time strictly after *after*, or ``None`` if never again."""
    expression = expression.strip()
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)

    if kind is ScheduleKind.CRON:
        zone = _resolve_timezone(timezone)
        local = after.astimezone(zone)
        try:
            iterator = croniter(expression, local)
            return iterator.get_next(datetime).astimezone(UTC)
        except (CroniterBadCronError, ValueError) as exc:
            raise SchedulerError(f"Cannot evaluate cron {expression!r}: {exc}") from exc

    if kind is ScheduleKind.INTERVAL:
        try:
            seconds = max(MIN_INTERVAL_SECONDS, float(expression))
        except ValueError as exc:
            raise SchedulerError(f"Invalid interval {expression!r}.") from exc
        return after + timedelta(seconds=seconds)

    if kind is ScheduleKind.ONCE:
        when = _parse_timestamp(expression)
        return when if when > after else None

    raise SchedulerError(f"Unknown schedule kind {kind!r}.")


def describe_schedule(task: ScheduledTask) -> str:
    """Human-readable summary, for CLI listings and tool output."""
    if task.kind is ScheduleKind.CRON:
        return f"cron '{task.expression}' ({task.timezone})"
    if task.kind is ScheduleKind.INTERVAL:
        seconds = float(task.expression or 0)
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"every {int(seconds // 3600)}h"
        if seconds >= 60 and seconds % 60 == 0:
            return f"every {int(seconds // 60)}m"
        return f"every {seconds:g}s"
    return f"once at {task.expression}"


def _resolve_timezone(name: str) -> tzinfo:
    """Resolve an IANA timezone name.

    ``zoneinfo`` reads the *system* tz database, which Windows does not have and
    slim Linux images often omit; the ``tzdata`` package supplies it and is a
    hard dependency for that reason. UTC falls back to the stdlib constant so a
    broken tz database can never break the default configuration.
    """
    wanted = name or "UTC"
    try:
        return ZoneInfo(wanted)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        if wanted.upper() == "UTC":
            return UTC
        raise SchedulerError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'UTC', "
            f"'Europe/London', or 'America/New_York'. If the name looks correct, "
            f"the tzdata package may be missing — reinstall tgagent."
        ) from exc


def _parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SchedulerError(
            f"{value!r} is not a valid ISO-8601 timestamp, e.g. '2026-09-01T08:00:00Z'."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
