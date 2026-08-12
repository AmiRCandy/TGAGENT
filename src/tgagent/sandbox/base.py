"""Sandbox interfaces and result types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: ``async (method, arguments) -> result``. Implemented by the bridge, which
#: forwards to the gateway. The sandbox never sees anything more capable.
RpcHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class ExecutionRequest:
    """A program to run."""

    code: str
    timeout: float = 60.0
    #: Short description used in logs and the audit trail.
    label: str = "agent-code"


@dataclass(slots=True)
class RpcRecord:
    """One Telegram call made by generated code — the execution trace."""

    method: str
    ok: bool
    duration_ms: float
    error: str | None = None


@dataclass(slots=True)
class ExecutionResult:
    """What the program produced."""

    ok: bool
    stdout: str = ""
    result: Any = None
    error: str | None = None
    traceback: str | None = None
    rpc_calls: int = 0
    duration_ms: float = 0.0
    timed_out: bool = False
    rpc_log: list[RpcRecord] = field(default_factory=list)

    def summary(self) -> str:
        """Compact rendering handed back to the model as the tool result."""
        lines: list[str] = []
        if self.stdout.strip():
            lines.append(self.stdout.rstrip())
        if self.result is not None:
            lines.append(f"result = {self.result!r}")
        if not self.ok:
            if self.timed_out:
                lines.append(f"ERROR: execution timed out after {self.duration_ms / 1000:.1f}s")
            else:
                lines.append(f"ERROR: {self.error}")
            if self.traceback:
                lines.append(self.traceback)
        if not lines:
            lines.append("(the program produced no output and set no `result`)")
        lines.append(f"[{self.rpc_calls} Telegram call(s), {self.duration_ms:.0f}ms]")
        return "\n".join(lines)


@runtime_checkable
class SandboxRunner(Protocol):
    """Executes model-generated code somewhere it cannot do harm."""

    #: Human-readable backend name, for logs and the ``sandbox status`` command.
    name: str

    async def execute(self, request: ExecutionRequest, rpc: RpcHandler) -> ExecutionResult: ...

    async def close(self) -> None: ...

    def describe_isolation(self) -> str:
        """One-paragraph, honest description of what this backend guarantees."""
        ...
