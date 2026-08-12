"""Code-execution sandboxing.

Backends are selected by ``sandbox.backend``; see ``docs/sandboxing.md`` for what
each one actually guarantees.
"""

from __future__ import annotations

from tgagent.config.settings import SandboxSettings
from tgagent.errors import SandboxError
from tgagent.sandbox.base import (
    ExecutionRequest,
    ExecutionResult,
    RpcHandler,
    RpcRecord,
    SandboxRunner,
)
from tgagent.sandbox.bridge import BridgeStats, GatewayBridge

__all__ = [
    "BridgeStats",
    "ExecutionRequest",
    "ExecutionResult",
    "GatewayBridge",
    "RpcHandler",
    "RpcRecord",
    "SandboxRunner",
    "create_sandbox",
]


def create_sandbox(settings: SandboxSettings, *, allow_unsafe: bool = False) -> SandboxRunner:
    """Instantiate the configured backend."""
    backend = settings.backend

    if backend == "subprocess":
        from tgagent.sandbox.subprocess_runner import SubprocessSandbox

        return SubprocessSandbox(settings)

    if backend == "docker":
        from tgagent.sandbox.docker_runner import DockerSandbox

        return DockerSandbox(settings)

    if backend == "inprocess":
        from tgagent.sandbox.inprocess import InProcessSandbox

        return InProcessSandbox(settings, allow_unsafe=allow_unsafe)

    if backend == "disabled":
        return DisabledSandbox()

    raise SandboxError(f"Unknown sandbox backend {backend!r}.")


class DisabledSandbox:
    """Refuses every execution, with an explanation the model can act on."""

    name = "disabled"

    def describe_isolation(self) -> str:
        return "Code execution is disabled by configuration."

    async def execute(self, request: ExecutionRequest, rpc: RpcHandler) -> ExecutionResult:
        return ExecutionResult(
            ok=False,
            error=(
                "Code execution is disabled in this deployment. Use the curated "
                "telegram_* tools instead, or ask the operator to enable it."
            ),
        )

    async def close(self) -> None:
        return None
