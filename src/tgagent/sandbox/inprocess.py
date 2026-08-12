"""In-process backend — for tests, and for nothing else.

Runs generated code in the *host* process. That means it shares the interpreter
with the Telegram session, the API keys, and the permission engine, so any
escape is total. It exists because tests need a fast, deterministic executor,
and because a developer debugging the tool layer should not have to reason about
subprocess plumbing at the same time.

It refuses to run unless explicitly enabled, and it logs a warning every time.
Selecting it in a real deployment is a configuration mistake, and the code says so.
"""

from __future__ import annotations

import asyncio
import io
import time
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from tgagent.config.settings import SandboxSettings
from tgagent.errors import SandboxError
from tgagent.observability.logging import get_logger
from tgagent.sandbox.base import ExecutionRequest, ExecutionResult, RpcHandler, RpcRecord

log = get_logger(__name__)


class InProcessSandbox:
    """Executes code in the current process. Unsafe by construction."""

    name = "inprocess"

    def __init__(self, settings: SandboxSettings, *, allow_unsafe: bool = False) -> None:
        if not allow_unsafe:
            raise SandboxError(
                "The in-process sandbox provides no isolation and must be enabled "
                "explicitly. Use the subprocess or docker backend instead."
            )
        self._settings = settings
        log.warning("sandbox.inprocess_enabled", detail="no isolation; do not use in production")

    def describe_isolation(self) -> str:
        return (
            "NONE. Code runs in the host process with full access to memory, the "
            "filesystem, the network, and the live Telegram session. Test-only."
        )

    async def execute(self, request: ExecutionRequest, rpc: RpcHandler) -> ExecutionResult:
        started = time.perf_counter()
        rpc_log: list[RpcRecord] = []
        loop = asyncio.get_running_loop()

        def call(method: str, arguments: dict[str, Any]) -> Any:
            """Bridge the synchronous program to the async RPC handler.

            The program body runs in a worker thread (see ``to_thread`` below),
            so scheduling back onto the loop is both necessary and safe.
            """
            call_started = time.perf_counter()
            future = asyncio.run_coroutine_threadsafe(rpc(method, arguments), loop)
            try:
                value = future.result(timeout=request.timeout)
            except Exception as exc:
                rpc_log.append(
                    RpcRecord(
                        method=method,
                        ok=False,
                        duration_ms=(time.perf_counter() - call_started) * 1000,
                        error=str(exc),
                    )
                )
                raise
            rpc_log.append(
                RpcRecord(
                    method=method,
                    ok=True,
                    duration_ms=(time.perf_counter() - call_started) * 1000,
                )
            )
            return value

        namespace: dict[str, Any] = {
            "__name__": "__agent__",
            "tg": _Proxy(call),
            "result": None,
        }

        buffer = io.StringIO()
        ok = True
        error: str | None = None

        def run() -> None:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                exec(compile(request.code, "<agent-code>", "exec"), namespace)  # noqa: S102

        try:
            await asyncio.wait_for(asyncio.to_thread(run), timeout=request.timeout)
        except TimeoutError:
            return ExecutionResult(
                ok=False,
                error=f"Execution exceeded the {request.timeout:.0f}s limit.",
                timed_out=True,
                duration_ms=(time.perf_counter() - started) * 1000,
                rpc_calls=len(rpc_log),
                rpc_log=rpc_log,
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        return ExecutionResult(
            ok=ok,
            stdout=buffer.getvalue()[: self._settings.max_output_bytes],
            result=namespace.get("result"),
            error=error,
            rpc_calls=len(rpc_log),
            duration_ms=(time.perf_counter() - started) * 1000,
            rpc_log=rpc_log,
        )

    async def close(self) -> None:
        return None


class _Proxy:
    """The same ``tg`` surface the real worker exposes."""

    __slots__ = ("_call",)

    def __init__(self, call: Any) -> None:
        self._call = call

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kwargs: self._call(name, kwargs)

    def invoke_raw(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._call(method, dict(params or {}))
