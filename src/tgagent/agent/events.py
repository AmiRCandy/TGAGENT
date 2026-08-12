"""Events emitted while a run is in progress.

The runtime pushes these to whatever is driving it. Interfaces subscribe and
render; the core imports nothing from any interface. This is what keeps the CLI
swappable for a web UI or an HTTP API without touching the agent loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    #: A chunk of the model's answer, when streaming.
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONTEXT_COMPACTED = "context_compacted"
    WARNING = "warning"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


@dataclass(slots=True, frozen=True)
class AgentEvent:
    kind: EventKind
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        kind: EventKind,
        text: str = "",
        data: dict[str, Any] | None = None,
        **extra: Any,
    ) -> AgentEvent:
        """Build an event.

        ``data`` is explicit rather than collected from ``**kwargs`` so that
        passing ``data={...}`` does the obvious thing instead of nesting it one
        level deeper — a mistake that is invisible until a consumer reads it.
        """
        return cls(kind=kind, text=text, data={**(data or {}), **extra})


@dataclass(slots=True)
class RunResult:
    """The outcome of one complete agent run."""

    run_id: str
    conversation_id: str
    answer: str
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    #: Set when the run stopped for a reason other than finishing normally.
    stopped_because: str | None = None
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.cancelled and self.stopped_because is None

    def summary_line(self) -> str:
        parts = [
            f"{self.steps} step(s)",
            f"{self.tool_calls} tool call(s)",
            f"{self.input_tokens + self.output_tokens} tokens",
            f"{self.duration_ms / 1000:.1f}s",
        ]
        line = " · ".join(parts)
        if self.stopped_because:
            line += f" · stopped: {self.stopped_because}"
        return line
