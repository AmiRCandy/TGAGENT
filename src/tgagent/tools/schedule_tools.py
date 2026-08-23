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
from tgagent.risk import PolicyDecision, RiskTier
from tgagent.scheduler.triggers import describe_schedule, next_run_after, validate_schedule
from tgagent.security.confirm import ConfirmationRequest
from tgagent.security.permissions import grantable
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
        "This is how any standing request becomes real — 'every minute', 'each "
        "morning', 'from now on' — and the prompt you store is a fresh instruction to "
        "a future run that will not remember this conversation, so write it "
        "self-contained: what to do, to which chat or account, and what to skip. "
        "Scheduled runs have nobody attached, so anything needing confirmation is "
        "refused every time; list the Telegram operations the task will perform in "
        "'needs' and this tool reports up front whether the policy permits them, which "
        "is the difference between finding out now and finding out never."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {
            "name": string_field("Unique short name, e.g. 'morning-review'."),
            "prompt": string_field(
                "The instruction to run each time, written to stand alone. A future "
                "run sees this and nothing else from the conversation you are in now."
            ),
            "kind": string_field(
                "Schedule type.", enum=["cron", "interval", "once"], default="cron"
            ),
            "expression": string_field(
                "For cron: a 5-field expression like '0 8 * * *' (08:00 daily). "
                "For interval: seconds between runs, e.g. '3600' — the minimum is 30. "
                "For once: an ISO-8601 timestamp."
            ),
            "timezone": string_field("IANA timezone for cron schedules.", default="UTC"),
            "needs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The Telegram operations each run will perform, named as methods — "
                    "'messages.SendMessage', 'account.UpdateProfile'. Checked against "
                    "the policy as an unattended run would see it, and reported back. "
                    "Give your best guess; a wrong guess costs nothing, and omitting "
                    "this is how a task comes to fail silently every time it fires."
                ),
                "maxItems": 20,
            },
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

        payload: dict[str, Any] = {}
        notes: list[str] = []
        if not context.scheduler_running:
            # The failure this exists to stop: a task is saved, the operator is
            # told it is set up, and nothing ever fires because the process
            # listening for commands is not the process that runs tasks. Saying it
            # here is the only moment anyone finds out cheaply.
            payload["nothing_will_run_it"] = (
                "No scheduler is running in this process, so this task will never "
                "fire. It has to be started with `tgagent listen` (which runs one by "
                "default) or `tgagent serve`."
            )
            notes.append(
                "Lead with nothing_will_run_it: the task is saved but dead until a "
                "scheduler runs. Do not describe it as working."
            )
        if blocked := unattended_blockers(context, arguments.get("needs")):
            granted_now, refused = await _seek_grants(task, blocked, context)
            if granted_now:
                payload["granted"] = granted_now
                notes.append(
                    "The owner granted these operations to this task alone. Say which "
                    "ones, that the grant covers only this task, and that deleting the "
                    "task ends it."
                )
            if refused:
                payload["will_fail_every_run"] = refused
                notes.append(
                    "The policy will refuse the operations in will_fail_every_run on "
                    "every run, because a scheduled run has nobody to confirm with. "
                    "Report that plainly, with the policy_fix lines, rather than "
                    "implying the task is fully set up."
                )
        if notes:
            payload["tell_the_user"] = " ".join(notes)

        await context.tasks.create(task)
        return ToolResult(
            content=json.dumps(
                {
                    "created": task.name,
                    "schedule": describe_schedule(task),
                    "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
                    **payload,
                },
                separators=(",", ":"),
            )
        )


async def _seek_grants(
    task: ScheduledTask, blocked: list[dict[str, str]], context: ToolContext
) -> tuple[list[str], list[dict[str, str]]]:
    """Ask the owner, once, whether this task may do what it needs to.

    The whole value is in *when* this happens. The owner is here, in the
    conversation, right now; the runs are at 04:00 for the next month. Asking now
    is the only moment a human can weigh "this job will change my profile every
    minute" against what they actually wanted.

    Returns ``(granted, still_refused)``. A refusal is not an error — the task is
    still created, because the owner may be about to edit the policy instead.
    """
    provider = context.confirmations
    engine = context.permissions
    if provider is None or engine is None or not context.interactive:
        return [], blocked
    if not getattr(provider, "interactive", False):
        return [], blocked

    granted_methods: list[str] = []
    refused: list[dict[str, str]] = []
    for entry in blocked:
        method = entry["method"]
        if (reason := grantable(method, engine.explain(method))) is not None:
            refused.append({**entry, "cannot_be_granted": reason})
            continue

        outcome = await provider.confirm(
            ConfirmationRequest(
                method=method,
                risk=RiskTier(entry["risk"]),
                summary=(
                    f"The scheduled task {task.name!r} ({describe_schedule(task)}) needs "
                    f"{method}, and its runs have nobody to confirm with. Granting it "
                    f"lets that task — and only that task — perform this operation "
                    f"unattended until you delete it."
                ),
                target=f"task/{task.name}",
                reason=entry["why"],
            )
        )
        if outcome.approved:
            granted_methods.append(method)
        else:
            refused.append({**entry, "not_granted": outcome.reason or "declined"})

    if granted_methods:
        # Stored on the task, so it is visible wherever the task is: in
        # `tgagent tasks list`, in the row itself, and in the reason attached to
        # every call it later permits.
        task.metadata["grants"] = granted_methods
        task.metadata["granted_at"] = datetime.now(UTC).isoformat()
        task.metadata["granted_by"] = "operator confirmation"
    return granted_methods, refused


def unattended_blockers(context: ToolContext, needs: Any) -> list[dict[str, str]]:
    """Which of *needs* an unattended run would be refused, and how to permit it.

    Asked at *setup* time on purpose. A recurring task that writes anything is
    refused on every single run under the default policy — a scheduled run has
    nobody to confirm with — and the only signal today is a line in a log nobody
    is reading at 04:00. Answering the question while the operator is still in
    the conversation turns a permanent silent failure into one sentence.
    """
    engine = context.permissions
    if engine is None or not isinstance(needs, list):
        return []

    permissions = context.settings.permissions
    blocked: list[dict[str, str]] = []
    for entry in needs[:20]:
        method = str(entry).strip()
        if not method:
            continue
        explanation = engine.explain(method)
        is_write = explanation.risk.at_least(RiskTier.EXTERNALLY_VISIBLE)

        if permissions.read_only_mode and is_write:
            reason = "read_only_mode is on, so every write is refused"
            effective = PolicyDecision.DENY
        else:
            # CONFIRM is not a maybe here: with nobody attached it becomes
            # whatever non_interactive_decision says, which is a refusal unless
            # the operator configured otherwise.
            effective = explanation.decision
            if effective is PolicyDecision.CONFIRM:
                effective = permissions.non_interactive_decision
            if effective is PolicyDecision.ALLOW:
                continue
            reason = (
                f"{explanation.risk.value} operations are {explanation.decision.value} "
                f"and no user is present to confirm"
            )

        blocked.append(
            {
                "method": method,
                "risk": explanation.risk.value,
                "decision": effective.value,
                "why": reason,
                "policy_fix": f"method_overrides:\n  {method}: allow",
            }
        )
    return blocked


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
                # What this task may do unattended that the policy would otherwise
                # refuse. Reported because "what is running on my account, and what
                # can it do?" is one question, not two.
                "grants": t.metadata.get("grants") or [],
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
