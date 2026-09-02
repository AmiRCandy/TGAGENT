"""Anthropic provider adapter.

Translates the neutral types in :mod:`tgagent.llm.base` to and from the Messages
API. Notable behaviours encoded here because getting them wrong is a 400 rather
than a degraded response:

* Sampling parameters (``temperature``/``top_p``) are **only sent when
  explicitly configured**. Current Opus/Sonnet-tier models reject them.
* ``thinking`` is sent as ``{"type": "adaptive"}``; the fixed ``budget_tokens``
  form has been removed from current models. Thinking is *not* disabled at
  ``xhigh``/``max`` effort, where an explicit disable is rejected.
* ``stop_reason == "refusal"`` is checked before reading ``content``, which can
  be empty on a refusal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from tgagent.config.settings import LLMSettings
from tgagent.errors import LLMConfigError, LLMError, LLMTransientError
from tgagent.llm.base import (
    Completion,
    GenerationParams,
    Message,
    Role,
    StopReason,
    StreamEvent,
    SystemPrompt,
    TextPart,
    ToolCallPart,
    ToolSpec,
    Usage,
    system_blocks,
)
from tgagent.llm.retry import retry_async
from tgagent.llm.tokens import estimate_text_tokens
from tgagent.observability.logging import get_logger

log = get_logger(__name__)

#: Effort levels at which an explicit "thinking off" is rejected by the API.
_EFFORT_REQUIRING_THINKING = frozenset({"xhigh", "max"})

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.OTHER,
}


class AnthropicProvider:
    """:class:`~tgagent.llm.base.LLMProvider` backed by the Anthropic SDK."""

    name = "anthropic"

    def __init__(self, settings: LLMSettings) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised by the extras test
            raise LLMConfigError(
                "The anthropic provider requires the SDK. Install it with "
                '`pip install "tgagent[anthropic]"`.'
            ) from exc

        self._anthropic = anthropic
        self._settings = settings
        self.model = settings.model
        self.context_window = settings.context_window

        kwargs: dict[str, Any] = {
            "timeout": settings.timeout,
            # Retries are handled by tgagent.llm.retry so that backoff, logging,
            # and budgets are uniform across providers.
            "max_retries": 0,
        }
        if settings.api_key is not None:
            kwargs["api_key"] = settings.api_key.get_secret_value()
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        # A bare constructor still resolves ANTHROPIC_API_KEY or a stored CLI
        # profile, so an unset api_key in our settings is not an error here.
        self._client = anthropic.AsyncAnthropic(**kwargs)

    # ------------------------------------------------------------- public ----
    async def complete(
        self,
        *,
        system: SystemPrompt,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> Completion:
        request = self._build_request(system, messages, tools, params)

        async def call() -> Any:
            try:
                return await self._client.messages.create(**request)
            except Exception as exc:
                raise self._translate_error(exc) from exc

        raw = await retry_async(
            call,
            max_retries=self._settings.max_retries,
            base_delay=self._settings.retry_base_delay,
            max_delay=self._settings.retry_max_delay,
            description=f"anthropic.messages.create({self.model})",
        )
        return self._to_completion(raw)

    async def stream(
        self,
        *,
        system: SystemPrompt,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> AsyncIterator[StreamEvent]:
        request = self._build_request(system, messages, tools, params)
        try:
            async with self._client.messages.stream(**request) as stream:
                async for event in stream:
                    parsed = self._parse_stream_event(event)
                    if parsed is not None:
                        yield parsed
                final = await stream.get_final_message()
        except Exception as exc:
            raise self._translate_error(exc) from exc

        completion = self._to_completion(final)
        for call in completion.tool_calls:
            yield StreamEvent(kind="tool_call", tool_call=call)
        yield StreamEvent(kind="done", completion=completion)

    def estimate_tokens(self, text: str) -> int:
        return estimate_text_tokens(text)

    async def aclose(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------ internals --
    def _build_request(
        self,
        system: SystemPrompt,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        params: GenerationParams | None,
    ) -> dict[str, Any]:
        p = params or GenerationParams(max_output_tokens=self._settings.max_output_tokens)

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": p.max_output_tokens,
            "messages": [self._encode_message(m) for m in messages if m.role is not Role.SYSTEM],
        }
        caching = self._settings.prompt_caching
        if blocks := system_blocks(system):
            # Blocks rather than a bare string, because that is the only encoding a
            # breakpoint attaches to — and the mark goes on the *first* block only.
            # Everything before a breakpoint is cached, so marking the stable block
            # and leaving the per-run tail after it is what lets one prefix serve
            # every run instead of only the steps within a run.
            prompt: list[dict[str, Any]] = [{"type": "text", "text": text} for text in blocks]
            prompt[0] = _cached(prompt[0], caching)
            request["system"] = prompt
        if tools:
            specs: list[dict[str, Any]] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            # The API caches everything *before* a breakpoint, and its prefix order
            # is tools → system → messages. One mark on the last tool therefore
            # covers the whole tool array, and the mark on the system block above
            # covers tools plus system. Both are byte-identical on every request
            # this process makes, in every chat, for every run — which is what
            # makes this the single largest saving available: ~6k tokens of tool
            # schema and ~3k of instructions stop being re-read every step.
            specs[-1] = _cached(specs[-1], caching)
            request["tools"] = specs

        if caching:
            _cache_history(request["messages"])
        if p.stop_sequences:
            request["stop_sequences"] = list(p.stop_sequences)

        # Only send sampling parameters when the operator asked for them.
        if p.temperature is not None:
            request["temperature"] = p.temperature
        if p.top_p is not None:
            request["top_p"] = p.top_p

        output_config: dict[str, Any] = {}
        if p.effort:
            output_config["effort"] = p.effort
        if output_config:
            request["output_config"] = output_config

        if p.thinking:
            request["thinking"] = {"type": "adaptive"}
        elif (p.effort or "") not in _EFFORT_REQUIRING_THINKING:
            # Disabling thinking is rejected above `high` effort; omitting the
            # parameter there is the only valid encoding.
            request["thinking"] = {"type": "disabled"}

        request.update(self._settings.extra)
        request.update(p.extra)
        return request

    @staticmethod
    def _encode_message(message: Message) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextPart):
                if part.text:
                    blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCallPart):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": part.id,
                        "name": part.name,
                        "input": part.arguments,
                    }
                )
            else:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": part.tool_call_id,
                    "content": part.content,
                }
                if part.is_error:
                    block["is_error"] = True
                blocks.append(block)

        if not blocks:
            # The API rejects an empty content array; a single space is the
            # least-surprising filler and costs one token.
            blocks.append({"type": "text", "text": " "})
        return {"role": message.role.value, "content": blocks}

    def _to_completion(self, raw: Any) -> Completion:
        stop_reason = _STOP_REASONS.get(getattr(raw, "stop_reason", "") or "", StopReason.OTHER)

        parts: list[Any] = []
        if stop_reason is StopReason.REFUSAL:
            details = getattr(raw, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            parts.append(
                TextPart(
                    "The model declined this request"
                    + (f" (category: {category})" if category else "")
                    + "."
                )
            )
        else:
            for block in getattr(raw, "content", None) or []:
                kind = getattr(block, "type", None)
                if kind == "text":
                    parts.append(TextPart(block.text))
                elif kind == "tool_use":
                    parts.append(
                        ToolCallPart(
                            id=block.id,
                            name=block.name,
                            arguments=dict(block.input) if block.input else {},
                        )
                    )
                elif kind == "thinking":
                    text = getattr(block, "thinking", "") or ""
                    if text:
                        parts.append(TextPart(text))

        usage_obj = getattr(raw, "usage", None)
        usage = Usage(
            input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        )
        return Completion(
            message=Message(role=Role.ASSISTANT, content=parts),
            stop_reason=stop_reason,
            usage=usage,
            model=getattr(raw, "model", self.model),
            raw=raw,
        )

    @staticmethod
    def _parse_stream_event(event: Any) -> StreamEvent | None:
        if getattr(event, "type", None) != "content_block_delta":
            return None
        delta = getattr(event, "delta", None)
        if delta is None:
            return None
        delta_type = getattr(delta, "type", None)
        if delta_type == "text_delta":
            return StreamEvent(kind="text", text=delta.text)
        if delta_type == "thinking_delta":
            return StreamEvent(kind="thinking", text=getattr(delta, "thinking", ""))
        return None

    def _translate_error(self, exc: Exception) -> Exception:
        """Map SDK exceptions onto the project's error taxonomy."""
        a = self._anthropic
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, a.RateLimitError):
            retry_after = _retry_after_header(exc)
            return LLMTransientError(f"Rate limited by Anthropic: {exc}", retry_after=retry_after)
        if isinstance(exc, (a.APIConnectionError, a.APITimeoutError)):
            return LLMTransientError(f"Anthropic connection problem: {exc}")
        if isinstance(exc, a.APIStatusError):
            if exc.status_code >= 500:
                return LLMTransientError(f"Anthropic server error {exc.status_code}: {exc}")
            if isinstance(exc, a.AuthenticationError):
                return LLMConfigError(
                    "Anthropic rejected the credentials. Set TGAGENT_LLM__API_KEY "
                    "or ANTHROPIC_API_KEY."
                )
            if exc.status_code == 404:
                # Anthropic answers an unknown model with 404 `not_found_error`.
                # That is a configuration mistake rather than a failure to reach
                # anything, and naming the model is what makes it fixable.
                return LLMConfigError(
                    f"Anthropic does not serve the model {self.model!r}. Set "
                    f"TGAGENT_LLM__MODEL to a current model id. It said: {exc}"
                )
            return LLMError(f"Anthropic request rejected ({exc.status_code}): {exc}")
        return LLMError(f"Anthropic request failed: {exc}")


def _retry_after_header(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ caching ---
#: Below this, a breakpoint is ignored by the API anyway (the minimum cacheable
#: prefix), so marking a conversation this short only risks paying the write
#: premium for nothing.
_MIN_CACHEABLE_HISTORY_TOKENS = 1024


def _cached(block: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Attach a cache breakpoint to *block*, if caching is on."""
    if not enabled:
        return block
    return {**block, "cache_control": {"type": "ephemeral"}}


def _cache_history(messages: list[dict[str, Any]]) -> None:
    """Mark the end of the conversation so far, in place.

    An agent loop re-sends every earlier turn on each step, so the naive cost of
    an *n*-step run is quadratic in its own history. A rolling breakpoint on the
    last block turns the next step's re-read into a cache hit and flattens that
    curve.

    A write costs more than a plain read, so this is only worth doing once there
    is enough history to cache: short runs — the majority — pay nothing extra.
    """
    if not messages:
        return
    size = estimate_text_tokens(
        "".join(
            str(block.get("text") or block.get("content") or "")
            for message in messages
            for block in message.get("content", [])
        )
    )
    if size < _MIN_CACHEABLE_HISTORY_TOKENS:
        return
    blocks = messages[-1].get("content") or []
    if blocks:
        blocks[-1] = _cached(blocks[-1], True)
