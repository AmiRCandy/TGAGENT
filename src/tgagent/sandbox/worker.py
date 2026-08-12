#!/usr/bin/env python3
"""The sandbox worker — runs model-generated code, holds nothing valuable.

This file is executed **by path**, as a standalone script, in a separate
process. It deliberately imports nothing from ``tgagent``: the child must not be
able to reach the project's modules, its configuration, its session file, or its
credentials, even by accident.

The security model in one sentence: *the worker has no capability to lose.*
There is no Telegram client here, no API key, no session, and no socket. The
only way out is a JSON-lines pipe to the host, where every request is
classified, authorised, confirmed, rate-limited, and audited exactly as if a
curated tool had made it.

Hardening applied on top of that (defence in depth, not the primary control):

* an import allow-list, enforced through a replacement ``__import__``;
* removal of ``open``, ``exec``, ``eval``, ``compile``, ``input``, ``breakpoint``
  and friends from the builtins the program sees;
* neutralisation of the already-imported ``socket`` and ``subprocess`` modules;
* POSIX resource limits (CPU, address space, file size, process count);
* a wall-clock timeout enforced by the *host*, which kills the process.

Known limitations are documented honestly in ``docs/sandboxing.md``. In
particular, on Windows there are no ``setrlimit`` equivalents, and a determined
escape from CPython-level restrictions is possible in any configuration — which
is precisely why the design assumes the escape and removes the prize.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import linecache
import os
import sys
import traceback
from typing import Any

PROTOCOL_VERSION = 1

# The real stdout carries protocol frames. It is captured here before anything
# else can rebind it, and user code never sees it.
_frames_out = sys.stdout
_frames_in = sys.stdin

_rpc_counter = 0
_rpc_calls_made = 0
_max_rpc_calls = 200


# --------------------------------------------------------------- framing ----
def _send(payload: dict[str, Any]) -> None:
    _frames_out.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    _frames_out.flush()


def _read_frame() -> dict[str, Any]:
    line = _frames_in.readline()
    if not line:
        raise SystemExit(0)  # host closed the pipe
    frame: dict[str, Any] = json.loads(line)
    return frame


# ------------------------------------------------------------------ rpc -----
class RpcError(Exception):
    """Raised inside generated code when the host refuses or fails a call."""


class PermissionDeniedError(RpcError):
    """The host's permission engine refused the operation."""


def _rpc(method: str, arguments: dict[str, Any]) -> Any:
    """Ask the host to perform a Telegram operation, and block for the answer."""
    global _rpc_counter, _rpc_calls_made

    if _rpc_calls_made >= _max_rpc_calls:
        raise RpcError(
            f"This program has already made {_rpc_calls_made} Telegram calls, which is "
            f"the configured maximum. Narrow the query or process fewer items."
        )

    _rpc_counter += 1
    _rpc_calls_made += 1
    call_id = str(_rpc_counter)

    _send({"type": "rpc", "id": call_id, "method": method, "arguments": arguments})

    while True:
        frame = _read_frame()
        if frame.get("type") != "rpc_result":
            # The host only ever sends rpc_result here; anything else is a bug
            # and silently ignoring it would desynchronise the stream.
            raise RpcError(f"Unexpected frame from host: {frame.get('type')!r}")
        if frame.get("id") != call_id:
            raise RpcError("Host replied to the wrong call; aborting.")
        if frame.get("ok"):
            return frame.get("result")
        error_type = frame.get("error_type", "")
        message = frame.get("error", "The Telegram call failed.")
        if error_type == "PermissionDenied":
            raise PermissionDeniedError(message)
        raise RpcError(message)


class TelegramProxy:
    """What generated code calls. Every attribute becomes an RPC.

    This is the whole point of the design: it *looks* like a Telegram client,
    but it holds no connection and no credential — only a pipe.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def method(**kwargs: Any) -> Any:
            return _rpc(name, kwargs)

        method.__name__ = name
        method.__doc__ = (
            f"Call the Telegram method {name!r} on the host. "
            f"Keyword arguments only. Returns plain JSON-compatible data."
        )
        return method

    def invoke_raw(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke any raw TL request, e.g. ``invoke_raw("messages.Search", {...})``.

        This is what makes the full Telegram API surface reachable. Use
        ``telegram_api_search`` to look up a method's parameters first.
        """
        return _rpc(method, dict(params or {}))

    def __repr__(self) -> str:
        return "<TelegramProxy: every call is policed by the host>"


# ------------------------------------------------------------- hardening ----
def _apply_resource_limits(cpu_seconds: int, memory_mb: int) -> list[str]:
    """Apply POSIX rlimits. Returns notes about what could not be applied.

    Every access goes through ``getattr``: the ``resource`` module is POSIX-only
    and its constants vary by platform, so naming them directly would both break
    type checking on Windows and crash on a Unix that lacks one of them.
    """
    notes: list[str] = []
    if os.name == "nt":
        return ["resource limits are unavailable on this platform (Windows)"]

    resource: Any = __import__("resource")
    setrlimit = resource.setrlimit

    def _set(name: str, soft: int, hard: int | None = None) -> None:
        which = getattr(resource, name, None)
        if which is None:
            return  # this platform has no such limit
        try:
            setrlimit(which, (soft, soft if hard is None else hard))
        except (ValueError, OSError) as exc:
            notes.append(f"could not set {name}: {exc}")

    if cpu_seconds > 0:
        # Soft below hard so SIGXCPU arrives first and Python can unwind.
        _set("RLIMIT_CPU", cpu_seconds, cpu_seconds + 5)
    if memory_mb > 0:
        _set("RLIMIT_AS", memory_mb * 1024 * 1024)
    _set("RLIMIT_NPROC", 0)  # no forking
    _set("RLIMIT_FSIZE", 0)  # no writing files
    _set("RLIMIT_CORE", 0)  # no core dumps
    return notes


def _neutralise_dangerous_modules() -> None:
    """Break modules already imported before the allow-list took effect.

    ``socket`` and ``subprocess`` are pulled in by the interpreter's own startup
    on some platforms, so blocking the *import* is not sufficient on its own.
    """
    blocked = "This capability is not available inside the sandbox."

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(blocked)

    for module_name, attributes in (
        ("socket", ("socket", "create_connection", "create_server", "socketpair")),
        ("subprocess", ("Popen", "run", "call", "check_output", "check_call")),
    ):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for attribute in attributes:
            if hasattr(module, attribute):
                with contextlib.suppress(AttributeError, TypeError):
                    setattr(module, attribute, _refuse)

    for name in ("system", "popen", "execv", "execve", "spawnv", "fork", "forkpty"):
        if hasattr(os, name):
            with contextlib.suppress(AttributeError, TypeError):
                setattr(os, name, _refuse)


def _build_import_hook(allowed: set[str]) -> Any:
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        root = name.split(".")[0]
        if root not in allowed:
            raise ImportError(
                f"Importing {root!r} is not allowed in the sandbox. "
                f"Allowed modules: {', '.join(sorted(allowed))}. "
                f"Use the `tg` object for anything Telegram-related."
            )
        return real_import(name, globals_, locals_, fromlist, level)

    return guarded_import


#: Builtins removed from what generated code can see. Each one is either a way
#: to reach the filesystem, a way to reach the interpreter, or a way to block.
# fmt: off  (columnar table: packed is far more readable than one item per line)
_REMOVED_BUILTINS = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "input",
        "breakpoint",
        "help",
        "exit",
        "quit",
        "memoryview",
        "globals",
        "vars",
        # Belt and braces: the `_`-prefix filter in _restricted_builtins already
        # excludes every dunder, so these two are listed for the reader's benefit.
        "__loader__",
        "__spec__",
    }
)
# fmt: on


#: Dunder builtins the language itself needs, re-added after the ``_`` filter.
#:
#: A ``class`` statement compiles to a call to ``__build_class__``, so without it
#: *no* class definition works — including ``@dataclass``, whose module is on the
#: default ``allowed_imports`` list. The failure is a bare
#: ``NameError: __build_class__ not found`` at the class statement, which is an
#: extremely confusing way to discover that. It grants nothing new: ``type`` is
#: already available, and three-argument ``type()`` builds the same classes.
_REQUIRED_DUNDERS = ("__build_class__",)


def _restricted_builtins(allowed_imports: set[str]) -> dict[str, Any]:
    safe = {
        name: getattr(builtins, name)
        for name in dir(builtins)
        if not name.startswith("_") and name not in _REMOVED_BUILTINS
    }
    for name in _REQUIRED_DUNDERS:
        safe[name] = getattr(builtins, name)
    safe["__import__"] = _build_import_hook(allowed_imports)
    safe["__name__"] = "builtins"
    return safe


# ----------------------------------------------------------------- output ---
class _CappedWriter(io.TextIOBase):
    """Collects printed output, stopping cleanly at a byte cap."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._size = 0
        self._limit = limit
        self.truncated = False

    def write(self, text: str) -> int:
        if self._size >= self._limit:
            self.truncated = True
            return len(text)
        remaining = self._limit - self._size
        chunk = text[:remaining]
        self._parts.append(chunk)
        self._size += len(chunk)
        if len(chunk) < len(text):
            self.truncated = True
        return len(text)

    def getvalue(self) -> str:
        out = "".join(self._parts)
        if self.truncated:
            out += f"\n… [output truncated at {self._limit} characters]"
        return out

    def writable(self) -> bool:
        return True


# ------------------------------------------------------------------ main ----
def main() -> int:
    global _max_rpc_calls

    frame = _read_frame()
    if frame.get("type") != "execute":
        _send({"type": "done", "ok": False, "error": "Expected an 'execute' frame."})
        return 1
    if frame.get("protocol_version") != PROTOCOL_VERSION:
        _send(
            {
                "type": "done",
                "ok": False,
                "error": f"Protocol mismatch: host speaks v{frame.get('protocol_version')}, "
                f"worker speaks v{PROTOCOL_VERSION}.",
            }
        )
        return 1

    code = frame.get("code", "")
    allowed = set(frame.get("allowed_imports") or [])
    _max_rpc_calls = int(frame.get("max_rpc_calls", 200))

    notes = _apply_resource_limits(
        int(frame.get("cpu_seconds", 60)), int(frame.get("memory_mb", 512))
    )
    _neutralise_dangerous_modules()

    capture = _CappedWriter(limit=200_000)
    program_globals: dict[str, Any] = {
        "__name__": "__agent__",
        "__builtins__": _restricted_builtins(allowed),
        "tg": TelegramProxy(),
        "RpcError": RpcError,
        "PermissionDeniedError": PermissionDeniedError,
        "result": None,
    }

    ok = True
    error: str | None = None
    tb: str | None = None

    # Registering the source with linecache means tracebacks show the model its
    # own code, with real line contents, instead of a bare line number.
    linecache.cache["<agent-code>"] = (len(code), None, code.splitlines(True), "<agent-code>")

    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = capture
    sys.stderr = capture
    try:
        compiled = compile(code, "<agent-code>", "exec")
        exec(compiled, program_globals)  # noqa: S102 - this is the entire purpose
    except SystemExit as exc:
        ok = exc.code in (0, None)
        if not ok:
            error = f"The program called exit({exc.code})."
    except BaseException as exc:  # noqa: BLE001 - report anything, including MemoryError
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        tb = _short_traceback()
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    _send(
        {
            "type": "done",
            "ok": ok,
            "result": _jsonable(program_globals.get("result")),
            "stdout": capture.getvalue(),
            "error": error,
            "traceback": tb,
            "rpc_calls": _rpc_calls_made,
            # Diagnostics for the host's log, deliberately kept out of stdout:
            # the model does not benefit from platform trivia in its tool result.
            "notes": notes,
        }
    )
    return 0 if ok else 1


def _short_traceback(limit: int = 8) -> str:
    """Traceback containing only the generated program's own frames.

    The worker's plumbing frames (``exec(compiled, ...)`` and the RPC shim) are
    noise that would mislead the model into "fixing" code it did not write.
    """
    exc_type, exc_value, tb = sys.exc_info()
    if exc_type is None:
        return ""

    frames = [f for f in traceback.extract_tb(tb) if f.filename == "<agent-code>"]
    parts: list[str] = []
    if frames:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_list(frames[-limit:]))
    parts.extend(traceback.format_exception_only(exc_type, exc_value))
    return "".join(parts)[:4000]


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Coerce the program's `result` into something the pipe can carry."""
    if depth > 6:
        return str(value)[:500]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(value.items())[:500]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in list(value)[:1000]]
    return str(value)[:2000]


if __name__ == "__main__":
    sys.exit(main())
