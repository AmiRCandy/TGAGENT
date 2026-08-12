"""The provider-agnostic LLM layer: types, retry, budgeting, registry."""

from __future__ import annotations

import pytest

from tgagent.config.settings import LLMSettings
from tgagent.errors import ConfigError, LLMConfigError, LLMError, LLMTransientError
from tgagent.llm.base import (
    Message,
    Role,
    StopReason,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from tgagent.llm.providers.fake import (
    FakeProvider,
    multi_tool_completion,
    text_completion,
    tool_call_completion,
)
from tgagent.llm.registry import available_providers, create_provider, register_provider
from tgagent.llm.retry import compute_delay, retry_async
from tgagent.llm.tokens import build_budget, estimate_messages_tokens, estimate_text_tokens


class TestMessageTypes:
    def test_round_trip_through_serialisation(self) -> None:
        original = Message(
            role=Role.ASSISTANT,
            content=[
                TextPart("thinking about it"),
                ToolCallPart(id="c1", name="get_messages", arguments={"limit": 5}),
            ],
        )
        restored = Message.from_dict(original.to_dict())
        assert restored.role is Role.ASSISTANT
        assert restored.text == "thinking about it"
        assert restored.tool_calls[0].name == "get_messages"
        assert restored.tool_calls[0].arguments == {"limit": 5}

    def test_tool_results_round_trip_with_the_error_flag(self) -> None:
        original = Message.tool_results(
            [ToolResultPart(tool_call_id="c1", content="denied", is_error=True)]
        )
        restored = Message.from_dict(original.to_dict())
        part = restored.content[0]
        assert isinstance(part, ToolResultPart)
        assert part.is_error

    def test_text_ignores_tool_traffic(self) -> None:
        message = Message(
            role=Role.ASSISTANT,
            content=[
                TextPart("hello "),
                ToolCallPart(id="1", name="t", arguments={}),
                TextPart("world"),
            ],
        )
        assert message.text == "hello world"

    def test_usage_adds(self) -> None:
        total = Usage(input_tokens=10, output_tokens=5) + Usage(input_tokens=3, output_tokens=2)
        assert (total.input_tokens, total.output_tokens, total.total) == (13, 7, 20)


class TestTokenEstimation:
    def test_scales_with_length(self) -> None:
        assert estimate_text_tokens("") == 0
        assert estimate_text_tokens("hello") >= 1
        assert estimate_text_tokens("x" * 400) > estimate_text_tokens("x" * 100)

    def test_message_estimate_includes_overhead(self) -> None:
        message = Message.user("hi")
        assert estimate_messages_tokens([message]) > estimate_text_tokens("hi")

    def test_budget_arithmetic(self) -> None:
        budget = build_budget(
            context_window=10_000,
            system="s" * 350,  # ~100 tokens
            tools=[ToolSpec(name="t", description="d", parameters={})],
            reserved_output_tokens=2_000,
        )
        assert budget.available_for_history < 10_000
        assert budget.fits(100)
        assert not budget.fits(budget.available_for_history + 1)
        assert budget.overflow(budget.available_for_history + 50) == 50

    def test_budget_cannot_go_negative(self) -> None:
        budget = build_budget(
            context_window=100, system="x" * 10_000, tools=[], reserved_output_tokens=5_000
        )
        assert budget.available_for_history == 0


class TestRetry:
    async def test_returns_on_first_success(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await retry_async(operation, max_retries=3, base_delay=0.001) == "ok"
        assert calls == 1

    async def test_retries_transient_then_succeeds(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise LLMTransientError("overloaded")
            return "recovered"

        result = await retry_async(operation, max_retries=5, base_delay=0.001, max_delay=0.01)
        assert result == "recovered"
        assert calls == 3

    async def test_gives_up_after_the_budget(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise LLMTransientError("still overloaded")

        with pytest.raises(LLMTransientError, match="after 3 attempts"):
            await retry_async(operation, max_retries=2, base_delay=0.001, max_delay=0.01)
        assert calls == 3

    async def test_non_transient_errors_are_not_retried(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise LLMError("bad request")

        with pytest.raises(LLMError):
            await retry_async(operation, max_retries=5, base_delay=0.001)
        assert calls == 1

    def test_delay_is_jittered_and_capped(self) -> None:
        for attempt in range(1, 8):
            delay = compute_delay(attempt, base=1.0, cap=10.0)
            assert 0.0 <= delay <= 10.0

    def test_retry_after_header_wins(self) -> None:
        assert compute_delay(1, base=1.0, cap=60.0, retry_after=7.5) == 7.5

    def test_retry_after_is_still_capped(self) -> None:
        assert compute_delay(1, base=1.0, cap=30.0, retry_after=9999) == 30.0


class TestRegistry:
    def test_builtin_providers_are_registered(self) -> None:
        for name in ("anthropic", "openai", "fake"):
            assert name in available_providers()

    def test_unknown_provider_lists_alternatives(self) -> None:
        with pytest.raises(LLMConfigError, match="Available:"):
            create_provider(LLMSettings(provider="does-not-exist"))

    def test_duplicate_registration_is_refused(self) -> None:
        with pytest.raises(LLMConfigError, match="already registered"):
            register_provider("fake", lambda s: FakeProvider())

    def test_replace_is_explicit(self) -> None:
        sentinel = FakeProvider(model="replaced")
        register_provider("temp-provider", lambda s: sentinel)
        register_provider("temp-provider", lambda s: sentinel, replace=True)
        assert create_provider(LLMSettings(provider="temp-provider")).model == "replaced"

    def test_fake_provider_is_creatable_from_settings(self) -> None:
        provider = create_provider(LLMSettings(provider="fake", model="m", context_window=8000))
        assert provider.model == "m"
        assert provider.context_window == 8000


class TestFakeProvider:
    async def test_replays_the_script_in_order(self) -> None:
        provider = FakeProvider(
            [text_completion("first"), tool_call_completion("get_messages", {"limit": 1})]
        )
        first = await provider.complete(system="s", messages=[Message.user("go")])
        second = await provider.complete(system="s", messages=[Message.user("go")])
        assert first.text == "first"
        assert second.tool_calls[0].name == "get_messages"
        assert second.stop_reason is StopReason.TOOL_USE

    async def test_falls_back_when_the_script_runs_out(self) -> None:
        provider = FakeProvider(default_text="all done")
        completion = await provider.complete(system="s", messages=[])
        assert completion.text == "all done"
        assert completion.stop_reason is StopReason.END_TURN

    async def test_records_requests_for_assertions(self) -> None:
        provider = FakeProvider()
        await provider.complete(
            system="the system prompt",
            messages=[Message.user("hello")],
            tools=[ToolSpec(name="t", description="d", parameters={})],
        )
        assert provider.requests[0].system == "the system prompt"
        assert provider.requests[0].tools[0].name == "t"

    async def test_callable_script_entries_see_the_request(self) -> None:
        provider = FakeProvider([lambda request: text_completion(f"saw {len(request.messages)}")])
        completion = await provider.complete(system="", messages=[Message.user("a")])
        assert completion.text == "saw 1"

    async def test_streaming_yields_deltas_then_done(self) -> None:
        provider = FakeProvider([text_completion("hello world, this is streamed")])
        kinds = []
        final = None
        async for event in provider.stream(system="", messages=[]):
            kinds.append(event.kind)
            if event.kind == "done":
                final = event.completion
        assert kinds.count("text") > 1
        assert kinds[-1] == "done"
        assert final is not None and final.text.startswith("hello world")

    async def test_multi_tool_completion(self) -> None:
        provider = FakeProvider([multi_tool_completion([("a", {}), ("b", {})])])
        completion = await provider.complete(system="", messages=[])
        assert [c.name for c in completion.tool_calls] == ["a", "b"]


class TestErrorClassification:
    """A configuration mistake must not look like a flaky network.

    The distinction is load-bearing in two places: `retry_async` only retries
    transient failures, and `AgentRuntime` only catches `LLMError` around a model
    call. An error on the wrong side of either line is either retried pointlessly
    or escapes the run loop entirely.
    """

    def test_a_config_error_is_also_an_llm_error(self) -> None:
        """Otherwise the agent loop's `except LLMError` never sees it.

        `LLMConfigError` used to derive from `ConfigError` alone, so a wrong API
        key or an unavailable model raised straight out of `AgentRuntime.run()`:
        no RunResult, no ERROR event, no RUN_FINISHED, and a UI waiting forever.
        """
        assert issubclass(LLMConfigError, LLMError)
        assert issubclass(LLMConfigError, ConfigError)
        assert not issubclass(LLMConfigError, LLMTransientError)

    def _provider(self) -> object:
        pytest.importorskip("openai")
        from tgagent.llm.providers.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            LLMSettings(
                provider="openai-compatible",
                model="kimi-k3",
                base_url="https://gateway.example/v1",
                api_key="sk-secret-value",
            )
        )

    def _status_error(self, status: int, body: object) -> Exception:
        pytest.importorskip("openai")
        import httpx
        import openai

        request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
        response = httpx.Response(status, request=request, json=body)
        return openai.APIStatusError("boom", response=response, body=body)

    def test_an_unavailable_model_names_the_model_we_asked_for(self) -> None:
        """The gateway cannot echo back a name it failed to resolve; we can."""
        error = self._provider()._translate_error(
            self._status_error(
                404,
                {
                    "error": {
                        "message": "Model 'N/A' not found. Check https://ava.al/models",
                        "code": "model_not_found",
                        "param": "model",
                    }
                },
            )
        )
        assert isinstance(error, LLMConfigError)
        assert "kimi-k3" in str(error)
        assert "TGAGENT_LLM__MODEL" in str(error)
        # The provider's own text is kept — it carries the catalogue link.
        assert "ava.al/models" in str(error)

    def test_a_404_that_is_not_about_the_model_blames_the_base_url(self) -> None:
        error = self._provider()._translate_error(
            self._status_error(404, {"error": {"message": "Not Found", "code": "not_found"}})
        )
        assert isinstance(error, LLMConfigError)
        assert "TGAGENT_LLM__BASE_URL" in str(error)
        assert "/v1" in str(error)

    def test_a_404_with_an_unparseable_body_still_classifies(self) -> None:
        """A gateway may answer with anything at all; the handler must not raise."""
        provider = self._provider()
        for body in ("plain string", [1, 2, 3], None, {"error": "flat"}):
            error = provider._translate_error(self._status_error(404, body))
            assert isinstance(error, LLMConfigError), body

    def test_the_api_key_never_appears_in_an_error(self) -> None:
        """These messages reach logs and, via the control bridge, a Telegram chat."""
        error = self._provider()._translate_error(
            self._status_error(404, {"error": {"message": "no", "code": "model_not_found"}})
        )
        assert "sk-secret-value" not in str(error)

    def test_a_server_error_is_still_transient(self) -> None:
        error = self._provider()._translate_error(
            self._status_error(503, {"error": {"message": "overloaded"}})
        )
        assert isinstance(error, LLMTransientError)

    def test_an_already_translated_error_passes_through(self) -> None:
        original = LLMConfigError("already classified")
        assert self._provider()._translate_error(original) is original
