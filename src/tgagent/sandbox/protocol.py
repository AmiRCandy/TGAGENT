"""The host↔worker wire protocol.

Newline-delimited JSON over the worker's stdin/stdout. Deliberately boring: it
has to be implementable by a dependency-free child process, debuggable by eye,
and impossible to desynchronise.

Frames
------
Host → worker
  ``execute``     start running a program

Worker → host
  ``rpc``         request a Telegram operation (correlated by ``id``)
  ``stdout``      a chunk of the program's printed output
  ``done``        the program finished (``ok`` says whether it raised)

Host → worker
  ``rpc_result``  the answer to an ``rpc`` frame

The worker never opens a socket and never holds a credential, so this pipe is
its only channel to the outside world — which is exactly why every frame it can
send is enumerated here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

PROTOCOL_VERSION: Final = 1

#: Refuse to parse a line longer than this. A worker that emits an enormous
#: frame is either broken or hostile; either way the host should not buffer it.
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024


class FrameType:
    EXECUTE = "execute"
    RPC = "rpc"
    RPC_RESULT = "rpc_result"
    STDOUT = "stdout"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class ExecuteFrame:
    """Host → worker: run this program."""

    code: str
    allowed_imports: list[str] = field(default_factory=list)
    max_rpc_calls: int = 200
    cpu_seconds: int = 60
    memory_mb: int = 512
    timeout: float = 60.0
    protocol_version: int = PROTOCOL_VERSION

    def encode(self) -> str:
        return encode_frame(
            {
                "type": FrameType.EXECUTE,
                "code": self.code,
                "allowed_imports": self.allowed_imports,
                "max_rpc_calls": self.max_rpc_calls,
                "cpu_seconds": self.cpu_seconds,
                "memory_mb": self.memory_mb,
                "timeout": self.timeout,
                "protocol_version": self.protocol_version,
            }
        )


@dataclass(slots=True)
class RpcFrame:
    """Worker → host: perform a Telegram operation."""

    id: str
    method: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class DoneFrame:
    """Worker → host: the program terminated."""

    ok: bool
    result: Any = None
    error: str | None = None
    traceback: str | None = None
    rpc_calls: int = 0


def encode_frame(payload: dict[str, Any]) -> str:
    """Serialise a frame to a single line.

    ``default=str`` guarantees a line is always produced: a frame that fails to
    encode would deadlock both sides waiting for each other.
    """
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


def decode_frame(line: str | bytes) -> dict[str, Any]:
    """Parse one line into a frame dict."""
    if isinstance(line, bytes):
        if len(line) > MAX_FRAME_BYTES:
            raise ValueError(f"Frame exceeds {MAX_FRAME_BYTES} bytes.")
        line = line.decode("utf-8", errors="replace")
    text = line.strip()
    if not text:
        raise ValueError("Empty frame.")
    payload = json.loads(text)
    if not isinstance(payload, dict) or "type" not in payload:
        raise ValueError("A frame must be a JSON object with a 'type' field.")
    return payload
