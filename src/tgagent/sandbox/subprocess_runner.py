"""Subprocess sandbox backend — the portable default.

Runs :mod:`tgagent.sandbox.worker` as a separate interpreter process, invoked
**by file path** rather than as a module, so the child cannot import anything
from ``tgagent``. The child is started with:

* ``-I`` (isolated mode): ignores ``PYTHONPATH``/``PYTHONHOME``, does not put
  the script's directory on ``sys.path``, and ignores user site-packages;
* a **scrubbed environment** containing only the handful of variables an
  interpreter needs to start — no ``ANTHROPIC_API_KEY``, no ``TGAGENT_*``, no
  ``TELEGRAM_*``;
* a fresh temporary working directory, removed afterwards;
* a wall-clock timeout enforced here, with escalation from terminate to kill.

What this does and does not give you is spelled out in ``docs/sandboxing.md``.
The short version: it is meaningful defence in depth, it is **not** a security
boundary against a determined CPython escape, and it does not need to be —
the child holds no credentials and its only exit is the policed RPC pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tgagent.config.settings import SandboxSettings
from tgagent.errors import SandboxError, SandboxProtocolError, SandboxTimeout
from tgagent.observability.logging import get_logger
from tgagent.sandbox.base import ExecutionRequest, ExecutionResult, RpcHandler, RpcRecord
from tgagent.sandbox.protocol import ExecuteFrame, FrameType, decode_frame, encode_frame

log = get_logger(__name__)

WORKER_PATH = Path(__file__).with_name("worker.py")

#: Environment variables the child is allowed to inherit. Everything else is
#: dropped, so a credential in the parent's environment cannot leak into code
#: the model wrote.
_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "WINDIR",
)


def build_child_environment() -> dict[str, str]:
    """A minimal environment for the worker process."""
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    # Deterministic hashing keeps repeated runs of the same program comparable.
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class SubprocessSandbox:
    """Runs generated code in an isolated child interpreter."""

    name = "subprocess"

    def __init__(self, settings: SandboxSettings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_rpc))

    def describe_isolation(self) -> str:
        pieces = [
            "Separate interpreter process started in isolated mode (-I) with a "
            "scrubbed environment and a throwaway working directory.",
            "No Telegram client, no credentials, and no network socket exist in "
            "the child; its only channel out is a JSON pipe to the policed gateway.",
            "Imports are restricted to an allow-list; open/exec/eval and the "
            "process-spawning helpers are removed or neutralised.",
        ]
        if os.name == "nt":
            pieces.append(
                "On Windows there is no setrlimit, so CPU and memory caps are NOT "
                "enforced — only the wall-clock timeout is. Use the docker backend "
                "for hard resource isolation."
            )
        else:
            pieces.append(
                "POSIX rlimits cap CPU time, address space, file size (0 — no writes), "
                "and process count (0 — no forking)."
            )
        return " ".join(pieces)

    async def execute(self, request: ExecutionRequest, rpc: RpcHandler) -> ExecutionResult:
        if not WORKER_PATH.exists():  # pragma: no cover - packaging failure
            raise SandboxError(f"The sandbox worker is missing at {WORKER_PATH}.")

        started = time.perf_counter()
        workdir = Path(tempfile.mkdtemp(prefix="tgagent-sbx-"))
        process: asyncio.subprocess.Process | None = None
        rpc_log: list[RpcRecord] = []

        try:
            process = await self._spawn(workdir)

            frame = ExecuteFrame(
                code=request.code,
                allowed_imports=list(self._settings.allowed_imports),
                max_rpc_calls=self._settings.max_rpc_calls,
                cpu_seconds=self._settings.max_cpu_seconds,
                memory_mb=self._settings.max_memory_mb,
                timeout=request.timeout,
            )
            assert process.stdin is not None
            process.stdin.write(frame.encode().encode("utf-8"))
            await process.stdin.drain()

            try:
                done = await asyncio.wait_for(
                    self._pump(process, rpc, rpc_log), timeout=request.timeout
                )
            except TimeoutError:
                await _terminate(process)
                duration = (time.perf_counter() - started) * 1000
                log.warning("sandbox.timeout", timeout=request.timeout, label=request.label)
                return ExecutionResult(
                    ok=False,
                    error=(
                        f"Execution exceeded the {request.timeout:.0f}s limit and was "
                        f"terminated. Reduce the amount of work, or fetch fewer messages."
                    ),
                    duration_ms=duration,
                    timed_out=True,
                    rpc_calls=len(rpc_log),
                    rpc_log=rpc_log,
                )

            duration = (time.perf_counter() - started) * 1000
            stderr = await _drain(process.stderr)
            if stderr.strip():
                log.warning("sandbox.stderr", text=stderr[:2000])

            return ExecutionResult(
                ok=bool(done.get("ok")),
                stdout=_cap(done.get("stdout") or "", self._settings.max_output_bytes),
                result=done.get("result"),
                error=done.get("error"),
                traceback=done.get("traceback"),
                rpc_calls=int(done.get("rpc_calls") or len(rpc_log)),
                duration_ms=duration,
                rpc_log=rpc_log,
            )

        except SandboxTimeout:
            raise
        except (OSError, ValueError) as exc:
            raise SandboxError(f"Could not start the sandbox process: {exc}") from exc
        finally:
            if process is not None and process.returncode is None:
                await _terminate(process)
            shutil.rmtree(workdir, ignore_errors=True)

    async def _spawn(self, workdir: Path) -> asyncio.subprocess.Process:
        """Start the worker. Overridden by the Docker backend."""
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(WORKER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=build_child_environment(),
        )

    async def _pump(
        self,
        process: asyncio.subprocess.Process,
        rpc: RpcHandler,
        rpc_log: list[RpcRecord],
    ) -> dict[str, Any]:
        """Relay frames until the worker reports it is done."""
        assert process.stdout is not None and process.stdin is not None

        while True:
            line = await process.stdout.readline()
            if not line:
                stderr = await _drain(process.stderr)
                raise SandboxProtocolError(
                    "The sandbox process exited without reporting a result."
                    + (f" stderr: {stderr[:1000]}" if stderr.strip() else "")
                )

            try:
                frame = decode_frame(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SandboxProtocolError(f"Unreadable frame from the sandbox: {exc}") from exc

            kind = frame.get("type")
            if kind == FrameType.DONE:
                return frame
            if kind != FrameType.RPC:
                raise SandboxProtocolError(f"Unexpected frame type {kind!r} from the sandbox.")

            reply = await self._handle_rpc(frame, rpc, rpc_log)
            process.stdin.write(encode_frame(reply).encode("utf-8"))
            await process.stdin.drain()

    async def _handle_rpc(
        self, frame: dict[str, Any], rpc: RpcHandler, rpc_log: list[RpcRecord]
    ) -> dict[str, Any]:
        call_id = frame.get("id", "")
        method = str(frame.get("method", ""))
        arguments = frame.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "type": FrameType.RPC_RESULT,
                "id": call_id,
                "ok": False,
                "error": "Arguments must be a JSON object.",
                "error_type": "ValueError",
            }

        started = time.perf_counter()
        try:
            async with self._semaphore:
                result = await rpc(method, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the program, not raised
            elapsed = (time.perf_counter() - started) * 1000
            rpc_log.append(
                RpcRecord(method=method, ok=False, duration_ms=elapsed, error=str(exc))
            )
            return {
                "type": FrameType.RPC_RESULT,
                "id": call_id,
                "ok": False,
                "error": getattr(exc, "user_message", None) or str(exc),
                "error_type": type(exc).__name__,
            }

        elapsed = (time.perf_counter() - started) * 1000
        rpc_log.append(RpcRecord(method=method, ok=True, duration_ms=elapsed))
        return {"type": FrameType.RPC_RESULT, "id": call_id, "ok": True, "result": result}

    async def close(self) -> None:
        return None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a child, escalating from polite to forceful."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=3.0)


async def _drain(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    try:
        data = await asyncio.wait_for(stream.read(64_000), timeout=1.0)
    except (TimeoutError, asyncio.IncompleteReadError):
        return ""
    return data.decode("utf-8", errors="replace")


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [truncated at {limit} characters]"
