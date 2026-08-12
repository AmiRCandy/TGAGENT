"""Context-window management and compaction.

A long agent run — paginating history, reading thousands of messages — will
exceed any context window. When the estimated history crosses a configurable
fraction of the budget, the oldest turns are replaced by a model-written summary
and the recent ones are kept verbatim.

Two details matter for correctness:

* **Tool-call pairs are never split.** Every provider rejects a conversation
  containing a ``tool_call`` with no matching ``tool_result`` (or the reverse).
  The split point is therefore snapped to a safe boundary, not taken literally
  from the "keep N recent" setting.
* **Compaction summaries are agent-authored**, so they carry no more authority
  than the model's own reasoning — but because they may quote Telegram content,
  the compaction prompt restates the trust rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tgagent.config.settings import AgentSettings
from tgagent.errors import ContextOverflowError
from tgagent.llm.base import (
    GenerationParams,
    LLMProvider,
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from tgagent.llm.tokens import build_budget, estimate_messages_tokens
from tgagent.observability.logging import get_logger

log = get_logger(__name__)

_SUMMARY_MARKER = "[Earlier conversation, summarised]"


@dataclass(slots=True)
class CompactionOutcome:
    compacted: bool
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    reason: str = ""


class ContextManager:
    """Keeps the conversation inside the model's context window."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: AgentSettings,
        *,
        compaction_prompt: str,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._compaction_prompt = compaction_prompt

    def estimate(self, messages: Sequence[Message]) -> int:
        return estimate_messages_tokens(messages)

    def needs_compaction(
        self, messages: Sequence[Message], *, system: str, tools: Sequence[ToolSpec]
    ) -> bool:
        budget = build_budget(
            context_window=self._provider.context_window,
            system=system,
            tools=tools,
            reserved_output_tokens=self._settings_max_output(),
        )
        return not budget.fits(
            self.estimate(messages), threshold=self._settings.compaction_threshold
        )

    async def compact(
        self,
        messages: list[Message],
        *,
        system: str,
        tools: Sequence[ToolSpec],
    ) -> tuple[list[Message], CompactionOutcome]:
        """Replace the oldest turns with a summary, if that is possible and useful."""
        tokens_before = self.estimate(messages)
        split = self._safe_split_point(messages)

        if split <= 0:
            # Nothing can be dropped without breaking a tool-call pair. This
            # happens when a single tool result is itself enormous.
            return messages, CompactionOutcome(
                compacted=False,
                reason="no safe split point; the recent turns alone exceed the budget",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                messages_before=len(messages),
                messages_after=len(messages),
            )

        older, recent = messages[:split], messages[split:]

        try:
            summary = await self._summarise(older)
        except Exception as exc:  # noqa: BLE001 - degrade rather than fail the run
            log.warning("context.summary_failed", error=str(exc))
            summary = self._mechanical_summary(older)

        compacted = [Message(role=Role.USER, content=[TextPart(summary)]), *recent]
        tokens_after = self.estimate(compacted)

        budget = build_budget(
            context_window=self._provider.context_window,
            system=system,
            tools=tools,
            reserved_output_tokens=self._settings_max_output(),
        )
        if not budget.fits(tokens_after):
            raise ContextOverflowError(
                f"Even after compaction the conversation needs ~{tokens_after} tokens "
                f"but only ~{budget.available_for_history} are available. Reduce the "
                f"page sizes you request, or start a new conversation."
            )

        log.info(
            "context.compacted",
            messages_before=len(messages),
            messages_after=len(compacted),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )
        return compacted, CompactionOutcome(
            compacted=True,
            messages_before=len(messages),
            messages_after=len(compacted),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            reason="context budget exceeded",
        )

    # ------------------------------------------------------------ internals --
    def _settings_max_output(self) -> int:
        # Reserve room for the model's own reply plus a margin for the tool
        # schemas the provider adds on its side.
        return max(2048, self._provider.context_window // 20)

    def _safe_split_point(self, messages: Sequence[Message]) -> int:
        """Largest index ≤ the keep-recent target that does not split a tool pair.

        Walks backwards from the target until it finds a message that neither
        contains tool calls awaiting results, nor is itself a tool-result turn.
        """
        keep = self._settings.compaction_keep_recent
        if len(messages) <= keep + 1:
            return 0

        candidate = len(messages) - keep
        while candidate > 0:
            if self._is_safe_boundary(messages, candidate):
                return candidate
            candidate -= 1
        return 0

    @staticmethod
    def _is_safe_boundary(messages: Sequence[Message], index: int) -> bool:
        """True if the conversation can be cut immediately before *index*."""
        following = messages[index]
        # A turn that carries tool results must keep the assistant turn that
        # requested them.
        if any(isinstance(p, ToolResultPart) for p in following.content):
            return False
        # And the message just before the cut must not be an unanswered request.
        preceding = messages[index - 1] if index > 0 else None
        return not (
            preceding is not None and any(isinstance(p, ToolCallPart) for p in preceding.content)
        )

    async def _summarise(self, older: Sequence[Message]) -> str:
        transcript = _render_transcript(older)
        completion = await self._provider.complete(
            system=self._compaction_prompt,
            messages=[Message.user(transcript)],
            tools=(),
            params=GenerationParams(max_output_tokens=2048, thinking=False),
        )
        text = completion.text.strip()
        if not text:
            raise ValueError("The model returned an empty summary.")
        return f"{_SUMMARY_MARKER}\n\n{text}"

    @staticmethod
    def _mechanical_summary(older: Sequence[Message]) -> str:
        """Fallback used when the summarisation call itself fails.

        Lossy, but it preserves the shape of what happened — which tools ran and
        what the user asked — so the run can still make progress.
        """
        lines: list[str] = [
            _SUMMARY_MARKER,
            "",
            "(Automatic summarisation was unavailable; this is a mechanical digest.)",
            "",
        ]
        for message in older:
            if message.role is Role.USER and message.text:
                lines.append(f"User asked: {message.text[:400]}")
            for part in message.content:
                if isinstance(part, ToolCallPart):
                    lines.append(f"Called tool: {part.name}({_short_args(part.arguments)})")
                elif isinstance(part, ToolResultPart):
                    lines.append(f"  → result: {part.content[:200]}")
            if message.role is Role.ASSISTANT and message.text:
                lines.append(f"Assistant said: {message.text[:300]}")
        return "\n".join(lines[:200])


def _render_transcript(messages: Sequence[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for part in message.content:
            if isinstance(part, TextPart) and part.text.strip():
                parts.append(f"[{message.role.value}] {part.text}")
            elif isinstance(part, ToolCallPart):
                parts.append(f"[tool call] {part.name}({_short_args(part.arguments)})")
            elif isinstance(part, ToolResultPart):
                marker = "error" if part.is_error else "result"
                parts.append(f"[tool {marker}] {part.content[:2000]}")
    return "\n\n".join(parts)


def _short_args(arguments: dict[str, object]) -> str:
    rendered = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in list(arguments.items())[:6])
    return rendered[:300]
