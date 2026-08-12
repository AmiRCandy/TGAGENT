"""A deterministic in-memory provider.

This is what makes agent behaviour testable. A test scripts the exact sequence of
completions the "model" will produce — including tool calls — and then asserts on
what the runtime did with them. No network, no flakiness, no API key.

It is shipped in the package rather than the test tree on purpose: it is also the
right provider for ``--dry-run`` demos and for reproducing a bug report without
credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tgagent.llm.base import (
    Completion,
    GenerationParams,
    Message,
    Role,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolSpec,
    Usage,
)
from tgagent.llm.tokens import estimate_text_tokens


@dataclass(slots=True)
class RecordedRequest:
    """Everything the runtime asked for, captured for assertions."""

    system: str
    messages: list[Message]
    tools: list[ToolSpec]
    params: GenerationParams | None


ScriptEntry = Completion | Callable[[RecordedRequest], Completion]


class FakeProvider:
    """:class:`~tgagent.llm.base.LLMProvider` that replays a script."""

    name = "fake"

    def __init__(
        self,
        script: Sequence[ScriptEntry] | None = None,
        *,
        model: str = "fake-model",
        context_window: int = 100_000,
        default_text: str = "Done.",
    ) -> None:
        self.model = model
        self.context_window = context_window
        self._script: list[ScriptEntry] = list(script or [])
        self._default_text = default_text
        self.requests: list[RecordedRequest] = []
        self.closed = False

    # ---------------------------------------------------------- scripting ----
    def push(self, entry: ScriptEntry) -> None:
        """Append one more scripted response."""
        self._script.append(entry)

    def push_text(self, text: str) -> None:
        self.push(text_completion(text, model=self.model))

    def push_tool_call(self, name: str, arguments: dict[str, Any], *, call_id: str = "") -> None:
        self.push(tool_call_completion(name, arguments, call_id=call_id, model=self.model))

    @property
    def remaining(self) -> int:
        return len(self._script)

    # ----------------------------------------------------------- provider ----
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> Completion:
        request = RecordedRequest(
            system=system, messages=list(messages), tools=list(tools), params=params
        )
        self.requests.append(request)

        if not self._script:
            # Exhausting the script means "the model decided it was finished",
            # which keeps tests that only script the interesting turns simple.
            return text_completion(self._default_text, model=self.model)

        entry = self._script.pop(0)
        return entry(request) if callable(entry) else entry

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> AsyncIterator[StreamEvent]:
        completion = await self.complete(
            system=system, messages=messages, tools=tools, params=params
        )
        if completion.text:
            # Chunked, so streaming consumers are exercised rather than handed
            # one monolithic event.
            text = completion.text
            for i in range(0, len(text), 24):
                yield StreamEvent(kind="text", text=text[i : i + 24])
        for call in completion.tool_calls:
            yield StreamEvent(kind="tool_call", tool_call=call)
        yield StreamEvent(kind="done", completion=completion)

    def estimate_tokens(self, text: str) -> int:
        return estimate_text_tokens(text)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------- helpers ----
_counter = 0


def _next_call_id() -> str:
    global _counter
    _counter += 1
    return f"fake_call_{_counter}"


def text_completion(text: str, *, model: str = "fake-model") -> Completion:
    return Completion(
        message=Message(role=Role.ASSISTANT, content=[TextPart(text)]),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=estimate_text_tokens(text)),
        model=model,
    )


def tool_call_completion(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "",
    text: str = "",
    model: str = "fake-model",
) -> Completion:
    parts: list[Any] = []
    if text:
        parts.append(TextPart(text))
    parts.append(ToolCallPart(id=call_id or _next_call_id(), name=name, arguments=arguments))
    return Completion(
        message=Message(role=Role.ASSISTANT, content=parts),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=8),
        model=model,
    )


def multi_tool_completion(
    calls: Sequence[tuple[str, dict[str, Any]]], *, model: str = "fake-model"
) -> Completion:
    """A single turn requesting several tools at once (parallel tool use)."""
    parts: list[Any] = [
        ToolCallPart(id=_next_call_id(), name=name, arguments=args) for name, args in calls
    ]
    return Completion(
        message=Message(role=Role.ASSISTANT, content=parts),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=8 * len(parts)),
        model=model,
    )


@dataclass(slots=True)
class FailingProvider:
    """Raises a scripted exception. For exercising the runtime's error paths."""

    error: Exception
    name: str = "failing"
    model: str = "failing-model"
    context_window: int = 100_000
    calls: int = field(default=0)

    async def complete(self, **_kwargs: Any) -> Completion:
        self.calls += 1
        raise self.error

    async def stream(self, **_kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        raise self.error
        yield  # pragma: no cover - unreachable, makes this an async generator

    def estimate_tokens(self, text: str) -> int:
        return estimate_text_tokens(text)

    async def aclose(self) -> None:
        return None
