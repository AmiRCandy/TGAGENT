"""OpenAI-compatible provider adapter.

Targets the Chat Completions wire format, which is implemented by OpenAI itself
and by a long tail of gateways and local servers (OpenRouter, Groq, Together,
vLLM, LM Studio, Ollama). Point ``llm.base_url`` at any of them and this adapter
works unchanged — that is the reason to implement this shape rather than a
single vendor's bespoke API.

It exists primarily to prove the abstraction is real: if the runtime works
against two genuinely different wire formats, the provider boundary is honest.
"""

from __future__ import annotations

import json
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
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from tgagent.llm.retry import retry_async
from tgagent.llm.tokens import estimate_text_tokens
from tgagent.observability.logging import get_logger
from tgagent.observability.redaction import redact_text

log = get_logger(__name__)

_FINISH_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAICompatibleProvider:
    """:class:`~tgagent.llm.base.LLMProvider` for Chat-Completions endpoints."""

    name = "openai"

    def __init__(self, settings: LLMSettings) -> None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMConfigError(
                "The openai provider requires the SDK. Install it with "
                '`pip install "tgagent[openai]"`.'
            ) from exc

        self._openai = openai
        self._settings = settings
        self.model = settings.model
        self.context_window = settings.context_window

        kwargs: dict[str, Any] = {"timeout": settings.timeout, "max_retries": 0}
        if settings.api_key is not None:
            kwargs["api_key"] = settings.api_key.get_secret_value()
        elif settings.base_url:
            # Local servers commonly ignore the key but the SDK insists on one.
            kwargs["api_key"] = "not-required"
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        self._client = openai.AsyncOpenAI(**kwargs)

    # ------------------------------------------------------------- public ----
    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> Completion:
        request = self._build_request(system, messages, tools, params)

        async def call() -> Any:
            try:
                return await self._client.chat.completions.create(**request)
            except Exception as exc:
                raise self._translate_error(exc) from exc

        raw = await retry_async(
            call,
            max_retries=self._settings.max_retries,
            base_delay=self._settings.retry_base_delay,
            max_delay=self._settings.retry_max_delay,
            description=f"openai.chat.completions.create({self.model})",
        )
        return self._to_completion(raw)

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: GenerationParams | None = None,
    ) -> AsyncIterator[StreamEvent]:
        request = self._build_request(system, messages, tools, params)
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        # Tool calls arrive as indexed fragments that must be reassembled.
        tool_fragments: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = Usage()

        try:
            response = await self._client.chat.completions.create(**request)
            async for chunk in response:
                if getattr(chunk, "usage", None):
                    usage = _usage_from(chunk.usage)
                for choice in getattr(chunk, "choices", None) or []:
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "content", None):
                        text_parts.append(delta.content)
                        yield StreamEvent(kind="text", text=delta.content)
                    for fragment in getattr(delta, "tool_calls", None) or []:
                        slot = tool_fragments.setdefault(
                            fragment.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if fragment.id:
                            slot["id"] = fragment.id
                        fn = getattr(fragment, "function", None)
                        if fn is not None:
                            if fn.name:
                                slot["name"] = fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
        except Exception as exc:
            raise self._translate_error(exc) from exc

        parts: list[Any] = []
        if text := "".join(text_parts):
            parts.append(TextPart(text))
        # Reassembled in index order: fragments arrive interleaved.
        calls = [
            ToolCallPart(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=_parse_arguments(slot["arguments"], slot["name"]),
            )
            for index, slot in enumerate(tool_fragments[i] for i in sorted(tool_fragments))
        ]
        parts.extend(calls)
        for call in calls:
            yield StreamEvent(kind="tool_call", tool_call=call)

        completion = Completion(
            message=Message(role=Role.ASSISTANT, content=parts),
            stop_reason=_FINISH_REASONS.get(finish_reason or "", StopReason.OTHER),
            usage=usage,
            model=self.model,
        )
        yield StreamEvent(kind="done", completion=completion)

    def estimate_tokens(self, text: str) -> int:
        return estimate_text_tokens(text)

    async def aclose(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------ internals --
    def _build_request(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        params: GenerationParams | None,
    ) -> dict[str, Any]:
        p = params or GenerationParams(max_output_tokens=self._settings.max_output_tokens)

        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        for message in messages:
            wire.extend(self._encode_message(message))

        request: dict[str, Any] = {
            "model": self.model,
            "messages": wire,
            "max_completion_tokens": p.max_output_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if p.temperature is not None:
            request["temperature"] = p.temperature
        if p.top_p is not None:
            request["top_p"] = p.top_p
        if p.stop_sequences:
            request["stop"] = list(p.stop_sequences)
        if p.effort:
            # Reasoning models accept this; others ignore or reject it, which is
            # why effort defaults to unset.
            request["reasoning_effort"] = p.effort

        request.update(self._settings.extra)
        request.update(p.extra)
        return request

    @staticmethod
    def _encode_message(message: Message) -> list[dict[str, Any]]:
        """One neutral message may become several wire messages.

        Chat Completions requires each tool result to be its own ``role: tool``
        message keyed by call id, whereas the neutral format groups them.
        """
        results = [p for p in message.content if isinstance(p, ToolResultPart)]
        if results:
            return [
                {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
                for r in results
            ]

        text = "".join(p.text for p in message.content if isinstance(p, TextPart))
        calls = [p for p in message.content if isinstance(p, ToolCallPart)]

        if message.role is Role.ASSISTANT:
            out: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in calls
                ]
            return [out]

        return [{"role": message.role.value, "content": text or " "}]

    def _to_completion(self, raw: Any) -> Completion:
        choice = (getattr(raw, "choices", None) or [None])[0]
        parts: list[Any] = []
        finish_reason = ""
        if choice is not None:
            finish_reason = choice.finish_reason or ""
            msg = choice.message
            if getattr(msg, "content", None):
                parts.append(TextPart(msg.content))
            parts.extend(
                ToolCallPart(
                    id=call.id,
                    name=call.function.name,
                    arguments=_parse_arguments(call.function.arguments, call.function.name),
                )
                for call in getattr(msg, "tool_calls", None) or []
            )

        return Completion(
            message=Message(role=Role.ASSISTANT, content=parts),
            stop_reason=_FINISH_REASONS.get(finish_reason, StopReason.OTHER),
            usage=_usage_from(getattr(raw, "usage", None)),
            model=getattr(raw, "model", self.model),
            raw=raw,
        )

    def _translate_error(self, exc: Exception) -> Exception:
        o = self._openai
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, o.RateLimitError):
            return LLMTransientError(f"Rate limited: {exc}")
        if isinstance(exc, (o.APIConnectionError, o.APITimeoutError)):
            return LLMTransientError(f"Connection problem: {exc}")
        if isinstance(exc, o.APIStatusError):
            if exc.status_code >= 500:
                return LLMTransientError(f"Server error {exc.status_code}: {exc}")
            if isinstance(exc, o.AuthenticationError):
                return LLMConfigError(
                    "The provider rejected the API key. Set TGAGENT_LLM__API_KEY."
                )
            if exc.status_code == 404:
                return self._explain_not_found(exc)
            return LLMError(f"Request rejected ({exc.status_code}): {exc}")
        return LLMError(f"Request failed: {exc}")

    def _explain_not_found(self, exc: Any) -> LLMConfigError:
        """Turn a 404 into something the operator can act on.

        A 404 is never retryable and never the model's fault: the endpoint does
        not have what was asked for. What makes it hard to diagnose is that the
        provider's message describes *its* view — gateways commonly cannot echo
        back a model name they failed to resolve, so the operator reads
        ``Model 'N/A' not found`` and has nothing connecting it to their own
        configuration. Naming the model *we* asked for, and where we asked, is
        the whole fix.
        """
        detail = _error_field(exc, "message")
        code = _error_field(exc, "code")
        param = _error_field(exc, "param")
        where = f" (base_url: {self._safe_base_url()})" if self._settings.base_url else ""
        # The provider's own text is kept: it often carries the useful part — a
        # link to its model catalogue, or a request id to quote to their support.
        said = f" The provider said: {detail[:400]}" if detail else ""

        if code == "model_not_found" or param == "model" or "model" in detail.lower():
            return LLMConfigError(
                f"The endpoint does not serve the model {self.model!r}{where}. Set "
                f"TGAGENT_LLM__MODEL to one it does.{said}"
            )
        return LLMConfigError(
            f"The endpoint returned 404 for {self.model!r}{where}. Either it does not "
            f"serve that model, or TGAGENT_LLM__BASE_URL is wrong — for an "
            f"OpenAI-compatible gateway it usually has to end in `/v1`.{said}"
        )

    def _safe_base_url(self) -> str:
        """The endpoint, with any known secret removed.

        Some gateways carry a token in the URL itself, and this string reaches an
        error message, a log line, and — through the control bridge — a Telegram
        chat. ``redact_text`` knows the secrets this process holds, the model API
        key among them, so it strips exactly those.
        """
        return redact_text(str(self._settings.base_url or ""))


def _error_field(exc: Any, name: str) -> str:
    """Read one field from an OpenAI-shaped error body, tolerating any shape.

    The body is whatever a third-party gateway chose to send, so every access is
    defensive: a provider answering with a bare string, a list, or nothing at all
    must not turn a useful 404 into an ``AttributeError`` raised from inside the
    error handler itself.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return str(body.get(name) or "")
    return str(error.get(name) or "")


def _usage_from(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    details = getattr(usage, "prompt_tokens_details", None)
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_tokens=getattr(details, "cached_tokens", 0) or 0 if details else 0,
    )


def _parse_arguments(raw: str, tool_name: str) -> dict[str, Any]:
    """Tool arguments arrive as a JSON *string* and are not always valid.

    A malformed payload is surfaced to the tool layer as an ``_error`` key rather
    than crashing the run: the model can see the problem and retry.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("llm.tool_arguments_invalid", tool=tool_name, error=str(exc))
        return {"_error": f"Malformed JSON arguments: {exc}", "_raw": raw[:500]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
