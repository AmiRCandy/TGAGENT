"""Provider-neutral LLM types and the :class:`LLMProvider` protocol.

Nothing above this module knows which vendor is in use. Adapters translate
between these types and their SDK; the agent runtime only ever sees these.

The shape is deliberately the *intersection* of what modern tool-calling APIs
offer — a message list, content blocks, tool specs, tool calls, tool results, a
stop reason and a usage record. Vendor-specific extras ride along in
``Completion.raw`` and ``GenerationParams.extra`` rather than leaking into the
common types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# ------------------------------------------------------------ content parts --
@dataclass(slots=True, frozen=True)
class TextPart:
    """Plain text within a message."""

    text: str
    type: str = "text"


@dataclass(slots=True, frozen=True)
class ToolCallPart:
    """The assistant asking for a tool to be run."""

    id: str
    name: str
    arguments: dict[str, Any]
    type: str = "tool_call"


@dataclass(slots=True, frozen=True)
class ToolResultPart:
    """The outcome of a tool call, fed back to the model.

    ``is_error`` matters: providers use it to tell the model the call failed
    rather than returning an error string it might mistake for data.
    """

    tool_call_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


ContentPart = TextPart | ToolCallPart | ToolResultPart


@dataclass(slots=True)
class Message:
    """One turn in the conversation sent to the provider."""

    role: Role
    content: list[ContentPart] = field(default_factory=list)

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role=Role.USER, content=[TextPart(text)])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role=Role.ASSISTANT, content=[TextPart(text)])

    @classmethod
    def tool_results(cls, results: Sequence[ToolResultPart]) -> Message:
        """Tool results always travel as a *user* turn — that is how the wire
        format works on every provider we target."""
        return cls(role=Role.USER, content=list(results))

    @property
    def text(self) -> str:
        """Concatenated text parts, ignoring tool traffic."""
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.content if isinstance(p, ToolCallPart)]

    def to_dict(self) -> dict[str, Any]:
        """Serialise for persistence. Round-trips through :meth:`from_dict`."""
        parts: list[dict[str, Any]] = []
        for part in self.content:
            if isinstance(part, TextPart):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCallPart):
                parts.append(
                    {
                        "type": "tool_call",
                        "id": part.id,
                        "name": part.name,
                        "arguments": part.arguments,
                    }
                )
            else:
                parts.append(
                    {
                        "type": "tool_result",
                        "tool_call_id": part.tool_call_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
        return {"role": self.role.value, "content": parts}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        parts: list[ContentPart] = []
        for raw in data.get("content", []):
            kind = raw.get("type")
            if kind == "text":
                parts.append(TextPart(raw.get("text", "")))
            elif kind == "tool_call":
                parts.append(
                    ToolCallPart(
                        id=raw.get("id", ""),
                        name=raw.get("name", ""),
                        arguments=raw.get("arguments", {}),
                    )
                )
            elif kind == "tool_result":
                parts.append(
                    ToolResultPart(
                        tool_call_id=raw.get("tool_call_id", ""),
                        content=raw.get("content", ""),
                        is_error=bool(raw.get("is_error", False)),
                    )
                )
        return cls(role=Role(data.get("role", "user")), content=parts)


# -------------------------------------------------------------------- tools --
@dataclass(slots=True, frozen=True)
class ToolSpec:
    """A tool as advertised to the model."""

    name: str
    description: str
    #: JSON Schema for the arguments object.
    parameters: dict[str, Any]


# --------------------------------------------------------------- completion --
class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    OTHER = "other"


@dataclass(slots=True, frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(slots=True)
class Completion:
    """What a provider returned for one request."""

    message: Message
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    #: The untouched provider payload, for debugging and provider-specific needs.
    raw: Any = None

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return self.message.tool_calls


# ------------------------------------------------------------------ streaming --
@dataclass(slots=True, frozen=True)
class StreamEvent:
    """An incremental update while a response is being generated.

    ``kind`` is one of ``text``, ``thinking``, ``tool_call``, or ``done``.
    Interfaces render ``text``; everything else is optional to display.
    """

    kind: str
    text: str = ""
    tool_call: ToolCallPart | None = None
    completion: Completion | None = None


# ----------------------------------------------------------------- request --
@dataclass(slots=True)
class GenerationParams:
    """Per-request generation settings.

    Every field is optional so an adapter can omit parameters its provider or
    model rejects — several current models 400 on ``temperature``, so sending it
    unconditionally would be a bug.
    """

    max_output_tokens: int = 4096
    temperature: float | None = None
    top_p: float | None = None
    effort: str | None = None
    thinking: bool = False
    stop_sequences: list[str] = field(default_factory=list)
    #: Passed through to the SDK call untouched.
    extra: dict[str, Any] = field(default_factory=dict)


#: A system prompt as the providers accept it: one string, or an ordered sequence
#: of blocks with the *stable* ones first. The split exists so a provider can mark
#: the unchanging prefix cacheable and leave the per-run tail outside it; one with
#: no such notion joins them and loses nothing.
SystemPrompt = str | Sequence[str]


def system_blocks(system: SystemPrompt) -> list[str]:
    """Normalise a system prompt to a list of non-empty blocks."""
    if isinstance(system, str):
        return [system] if system else []
    return [block for block in system if block]


def system_text(system: SystemPrompt) -> str:
    """Flatten a system prompt for a provider that takes a single string."""
    return "\n\n".join(system_blocks(system))


@runtime_checkable
class LLMProvider(Protocol):
    """What the agent runtime needs from a model provider."""

    #: Registry key, e.g. ``"anthropic"``.
    name: str
    #: The model id in use.
    model: str
    #: Total context window in tokens, used for budgeting and compaction.
    context_window: int

    async def complete(
        self,
        *,
        system: SystemPrompt,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> Completion:
        """Produce one assistant turn, possibly containing tool calls."""
        ...

    def stream(
        self,
        *,
        system: SystemPrompt,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the same thing. The final event has ``kind == "done"`` and
        carries the assembled :class:`Completion`."""
        ...

    def estimate_tokens(self, text: str) -> int:
        """Approximate token count. Used for budgeting, never for billing."""
        ...

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        ...
