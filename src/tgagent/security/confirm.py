"""Confirmation providers.

When the policy says CONFIRM, *something* has to ask. That something differs by
interface — a CLI prompts on the terminal, a web UI opens a modal, a scheduled
run has nobody to ask — so it is an interface, not a function.

Every provider must be non-blocking and must honour a timeout. A confirmation
prompt that hangs forever would wedge the agent loop, so the timeout resolves to
"declined" rather than waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from tgagent.observability.logging import get_logger
from tgagent.risk import RiskTier

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class ConfirmationRequest:
    """What the user is being asked to approve."""

    method: str
    risk: RiskTier
    summary: str
    target: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def render(self) -> str:
        lines = [f"Operation : {self.method}", f"Risk      : {self.risk.value}"]
        if self.target:
            lines.append(f"Target    : {self.target}")
        if self.reason:
            lines.append(f"Policy    : {self.reason}")
        if self.summary:
            lines.append(f"Details   : {self.summary}")
        return "\n".join(lines)


@dataclass(slots=True, frozen=True)
class ConfirmationOutcome:
    approved: bool
    reason: str = ""
    #: True when the user chose "yes to everything like this for this run".
    remember: bool = False


@runtime_checkable
class ConfirmationProvider(Protocol):
    """Asks a human whether an operation may proceed."""

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome: ...

    @property
    def interactive(self) -> bool:
        """False when nobody can answer, so the engine can skip prompting."""
        ...


class AutoDenyConfirmation:
    """Refuses everything. The correct default for unattended execution."""

    interactive = False

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        log.info("confirm.auto_denied", method=request.method, risk=request.risk.value)
        return ConfirmationOutcome(
            approved=False,
            reason="No interactive user is attached; confirmation was denied automatically.",
        )


class AutoApproveConfirmation:
    """Approves everything.

    Only for tests and for deliberately unattended deployments where the policy
    file has already narrowed what can be attempted. It logs loudly because
    silently auto-approving destructive operations is exactly the failure this
    project is meant to avoid.
    """

    interactive = True

    def __init__(self, *, record: list[ConfirmationRequest] | None = None) -> None:
        self.recorded: list[ConfirmationRequest] = record if record is not None else []

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        self.recorded.append(request)
        log.warning(
            "confirm.auto_approved",
            method=request.method,
            risk=request.risk.value,
            target=request.target,
        )
        return ConfirmationOutcome(approved=True, reason="Auto-approval is enabled.")


class CallbackConfirmation:
    """Delegates to an async callback supplied by the interface."""

    def __init__(
        self,
        callback: Callable[[ConfirmationRequest], Awaitable[ConfirmationOutcome]],
        *,
        timeout: float = 300.0,
        interactive: bool = True,
    ) -> None:
        self._callback = callback
        self._timeout = timeout
        self._interactive = interactive
        #: Methods the user chose to approve for the remainder of the run.
        self._remembered: set[str] = set()

    @property
    def interactive(self) -> bool:
        return self._interactive

    def reset(self) -> None:
        """Clear "remember for this run" grants. Called between runs."""
        self._remembered.clear()

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        if request.method in self._remembered:
            return ConfirmationOutcome(
                approved=True, reason="Approved earlier in this run for this operation type."
            )
        try:
            outcome = await asyncio.wait_for(self._callback(request), timeout=self._timeout)
        except TimeoutError:
            log.warning("confirm.timeout", method=request.method, timeout=self._timeout)
            return ConfirmationOutcome(
                approved=False,
                reason=f"No answer within {self._timeout:.0f}s; treated as declined.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken prompt must not allow the action
            log.error("confirm.callback_failed", method=request.method, error=str(exc))
            return ConfirmationOutcome(
                approved=False, reason=f"The confirmation prompt failed: {exc}"
            )

        if outcome.approved and outcome.remember:
            self._remembered.add(request.method)
        return outcome
