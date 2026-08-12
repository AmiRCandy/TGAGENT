"""Token estimation and context budgeting.

Deliberately an *estimate*. Exact counting means a network round trip per
measurement, which is far too expensive for a compaction check that runs on
every step. The estimator is tuned to over-count slightly: a compaction that
fires a little early is harmless, one that fires too late overflows the window.

If a provider offers cheap local counting it can override
:meth:`~tgagent.llm.base.LLMProvider.estimate_tokens`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tgagent.llm.base import Message, TextPart, ToolCallPart, ToolResultPart, ToolSpec

#: Empirically ~3.6 chars/token for English prose across current tokenisers;
#: 3.5 keeps the estimate on the conservative side.
_CHARS_PER_TOKEN = 3.5

#: Per-message envelope (role markers, delimiters) charged by every provider.
_MESSAGE_OVERHEAD = 4

#: Tool-call and tool-result blocks carry more structural JSON than plain text.
_TOOL_BLOCK_OVERHEAD = 12


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a plain string."""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN) + 1)


def estimate_message_tokens(message: Message) -> int:
    """Estimate tokens for one message including its structural overhead."""
    total = _MESSAGE_OVERHEAD
    for part in message.content:
        if isinstance(part, TextPart):
            total += estimate_text_tokens(part.text)
        elif isinstance(part, ToolCallPart):
            total += _TOOL_BLOCK_OVERHEAD + estimate_text_tokens(part.name)
            total += estimate_text_tokens(repr(part.arguments))
        elif isinstance(part, ToolResultPart):
            total += _TOOL_BLOCK_OVERHEAD + estimate_text_tokens(part.content)
    return total


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_tools_tokens(tools: Sequence[ToolSpec]) -> int:
    """Tool schemas are re-sent on every request and are rarely small."""
    total = 0
    for tool in tools:
        total += estimate_text_tokens(tool.name) + estimate_text_tokens(tool.description)
        total += estimate_text_tokens(repr(tool.parameters)) + 8
    return total


@dataclass(slots=True, frozen=True)
class TokenBudget:
    """How much of the window is spoken for, and what is left.

    ``available_for_history`` is the number the compactor cares about: the
    window minus the system prompt, the tool schemas, and the space reserved for
    the model's own output.
    """

    context_window: int
    system_tokens: int
    tools_tokens: int
    reserved_output_tokens: int

    @property
    def available_for_history(self) -> int:
        used = self.system_tokens + self.tools_tokens + self.reserved_output_tokens
        return max(0, self.context_window - used)

    def fits(self, history_tokens: int, *, threshold: float = 1.0) -> bool:
        """True if *history_tokens* fits within *threshold* of the available space."""
        return history_tokens <= self.available_for_history * threshold

    def overflow(self, history_tokens: int) -> int:
        """Tokens by which the history exceeds the budget (0 if it fits)."""
        return max(0, history_tokens - self.available_for_history)


def build_budget(
    *,
    context_window: int,
    system: str,
    tools: Sequence[ToolSpec],
    reserved_output_tokens: int,
) -> TokenBudget:
    return TokenBudget(
        context_window=context_window,
        system_tokens=estimate_text_tokens(system),
        tools_tokens=estimate_tools_tokens(tools),
        reserved_output_tokens=reserved_output_tokens,
    )
