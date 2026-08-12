"""Scheduling tools.

Lets the agent set up recurring work — "every morning, review my unread messages
and tell me what needs a reply". A scheduled task is stored as data (a cron
expression plus a prompt), so it survives restarts and upgrades.

Scheduled runs are **non-interactive**: nobody is there to answer a confirmation
prompt, so ``permissions.non_interactive_decision`` (deny, by default) applies to
anything that would otherwise ask. A task that needs to send messages must be
granted that explicitly in the policy file, not by the agent scheduling itself
more permission than it has.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tgagent.errors import ToolInputError
from tgagent.risk import RiskTier
from tgagent.scheduler.triggers import describe_schedule, next_run_after, validate_schedule
from tgagent.storage.models import ScheduledTask, ScheduleKind
from tgagent.tools.base import (
    ToolContext,
    ToolResult,
    boolean_field,
    object_schema,
    require,
    string_field,
)


class ScheduleCreateTool:
    name = "schedule_create"
    description = (
        "Create a recurring or one-off task that runs this agent with a fixed prompt. "
        "Use for standing requests like a daily unread review. Scheduled runs have no "
        "user present, so operations that would need confirmation are denied unless "
        "the policy allows them — say so when reporting back."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {
            "name": string_field("Unique short name, e.g. 'morning-review'."),
            "prompt": string_field("The instruction to run each time."),
            "kind": string_field(
                "Schedule type.", enum=["cron", "interval", "once"], default="cron"
            ),
            "expression": string_field(
                "For cron: a 5-field expression like '0 8 * * *' (08:00 daily). "
                "For interval: seconds between runs, e.g. '3600'. "
                "For once: an ISO-8601 timestamp."
            ),
            "timezone": string_field("IANA timezone for cron schedules.", default="UTC"),
        },
        required=["name", "prompt", "expression"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.tasks is None or not context.settings.features.scheduling:
            return ToolResult.error("Scheduling is disabled in this deployment.")

        name = str(require(arguments, "name", self.name)).strip()[:100]
        if await context.tasks.get_by_name(name):
            raise ToolInputError(
                f"A task named {name!r} already exists. Use schedule_delete first, or "
                f"choose a different name."
            )

        kind = ScheduleKind(str(arguments.get("kind") or "cron").lower())
        expression = str(require(arguments, "expression", self.name)).strip()
        timezone = str(arguments.get("timezone") or context.settings.scheduler.default_timezone)

        validate_schedule(kind, expression, timezone)

        now = datetime.now(UTC)
        task = ScheduledTask(
            name=name,
            prompt=str(require(arguments, "prompt", self.name))[:8000],
            kind=kind,
            expression=expression,
            timezone=timezone,
            enabled=True,
            next_run_at=next_run_after(kind, expression, timezone, now),
            metadata={"created_by": "agent", "conversation_id": context.conversation_id},
        )
        await context.tasks.create(task)

        return ToolResult(
            content=json.dumps(
                {
                    "created": task.name,
                    "schedule": describe_schedule(task),
                    "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                },
                separators=(",", ":"),
            )
        )


class ScheduleListTool:
    name = "schedule_list"
    description = (
        "List the scheduled tasks that exist, with their schedules, whether they are "
        "enabled, when they next run, and how the last run went. Check this before "
        "creating a task, so you do not duplicate one that already exists."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {"enabled_only": boolean_field("Only list enabled tasks.", default=False)}
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.tasks is None or not context.settings.features.scheduling:
            return ToolResult.error("Scheduling is disabled in this deployment.")

        tasks = await context.tasks.list_all(enabled_only=bool(arguments.get("enabled_only")))
        payload = [
            {
                "name": t.name,
                "schedule": describe_schedule(t),
                "enabled": t.enabled,
                "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
                "last_status": t.last_status.value if t.last_status else None,
                "last_error": t.last_error,
                "run_count": t.run_count,
                "prompt": t.prompt[:300],
            }
            for t in tasks
        ]
        return ToolResult(
            content=json.dumps({"tasks": payload, "count": len(payload)}, separators=(",", ":"))
        )


class ScheduleDeleteTool:
    name = "schedule_delete"
    description = (
        "Delete a scheduled task by name, or disable it while keeping its definition "
        "so it can be re-enabled later. Prefer disabling when the user may want it "
        "back."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {
            "name": string_field("The task name."),
            "disable_only": boolean_field(
                "Disable instead of deleting, so it can be re-enabled later.", default=False
            ),
        },
        required=["name"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.tasks is None or not context.settings.features.scheduling:
            return ToolResult.error("Scheduling is disabled in this deployment.")

        name = str(require(arguments, "name", self.name))
        task = await context.tasks.get_by_name(name)
        if task is None:
            raise ToolInputError(f"No scheduled task named {name!r}.")

        if arguments.get("disable_only"):
            task.enabled = False
            task.next_run_at = None
            await context.tasks.update(task)
            return ToolResult(content=json.dumps({"disabled": name}, separators=(",", ":")))

        await context.tasks.delete(task.id)
        return ToolResult(content=json.dumps({"deleted": name}, separators=(",", ":")))


def build_schedule_tools() -> list[Any]:
    return [ScheduleListTool(), ScheduleCreateTool(), ScheduleDeleteTool()]
