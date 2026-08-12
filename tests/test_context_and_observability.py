"""Context compaction, token budgeting, and log redaction."""

from __future__ import annotations

import pytest

from tgagent.agent.context import ContextManager
from tgagent.agent.prompts import COMPACTION_PROMPT, build_system_prompt
from tgagent.config.settings import AgentSettings, Settings
from tgagent.errors import ContextOverflowError
from tgagent.llm.base import (
    Message,
    Role,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)
from tgagent.llm.providers.fake import FakeProvider, text_completion
from tgagent.observability.redaction import (
    PLACEHOLDER,
    SecretRegistry,
    redact_text,
    redact_value,
)


def _long_history(turns: int = 20, size: int = 4000) -> list[Message]:
    history: list[Message] = []
    for i in range(turns):
        history.append(Message.user(f"question {i} " + "x" * size))
        history.append(Message.assistant(f"answer {i} " + "y" * size))
    return history


class TestCompaction:
    def _manager(self, provider: FakeProvider, **overrides: object) -> ContextManager:
        settings = AgentSettings(**overrides)  # type: ignore[arg-type]
        return ContextManager(provider, settings, compaction_prompt=COMPACTION_PROMPT)

    def test_short_conversations_are_left_alone(self) -> None:
        provider = FakeProvider(context_window=200_000)
        manager = self._manager(provider)
        assert not manager.needs_compaction(
            [Message.user("hello")], system="sys", tools=[]
        )

    def test_long_conversations_trigger_compaction(self) -> None:
        provider = FakeProvider(context_window=8_000)
        manager = self._manager(provider)
        assert manager.needs_compaction(_long_history(), system="sys", tools=[])

    async def test_compaction_summarises_and_keeps_recent_turns(self) -> None:
        provider = FakeProvider(
            [text_completion("Summary: the user asked about January.")],
            context_window=20_000,
        )
        manager = self._manager(provider, compaction_keep_recent=4)
        history = _long_history(turns=10, size=200)

        compacted, outcome = await manager.compact(history, system="sys", tools=[])

        assert outcome.compacted
        assert len(compacted) < len(history)
        assert "Summary: the user asked about January." in compacted[0].text
        # The most recent turns survive verbatim.
        assert compacted[-1].text == history[-1].text

    async def test_compaction_reduces_the_token_estimate(self) -> None:
        provider = FakeProvider([text_completion("short summary")], context_window=50_000)
        manager = self._manager(provider, compaction_keep_recent=4)
        history = _long_history(turns=12, size=2_000)

        compacted, outcome = await manager.compact(history, system="s", tools=[])
        assert outcome.tokens_after < outcome.tokens_before
        assert manager.estimate(compacted) < manager.estimate(history)

    async def test_tool_call_pairs_are_never_split(self) -> None:
        # Splitting a tool_call from its tool_result is a hard 400 on every
        # provider, so the split point must snap to a safe boundary.
        provider = FakeProvider([text_completion("summary")], context_window=20_000)
        manager = self._manager(provider, compaction_keep_recent=3)

        history: list[Message] = [Message.user("start")]
        for i in range(8):
            history.append(
                Message(
                    role=Role.ASSISTANT,
                    content=[ToolCallPart(id=f"c{i}", name="tool", arguments={})],
                )
            )
            history.append(
                Message.tool_results(
                    [ToolResultPart(tool_call_id=f"c{i}", content="x" * 400)]
                )
            )

        compacted, _ = await manager.compact(history, system="s", tools=[])

        requested = {
            p.id for m in compacted for p in m.content if isinstance(p, ToolCallPart)
        }
        answered = {
            p.tool_call_id
            for m in compacted
            for p in m.content
            if isinstance(p, ToolResultPart)
        }
        assert answered <= requested, "a tool result lost its request"

    async def test_summarisation_failure_falls_back_to_a_mechanical_digest(self) -> None:
        from tgagent.llm.providers.fake import FailingProvider
        from tgagent.errors import LLMError

        class Hybrid(FailingProvider):
            context_window = 20_000

        provider = Hybrid(error=LLMError("summariser down"))
        manager = ContextManager(
            provider,  # type: ignore[arg-type]
            AgentSettings(compaction_keep_recent=3),
            compaction_prompt=COMPACTION_PROMPT,
        )
        compacted, outcome = await manager.compact(
            _long_history(turns=8, size=100), system="s", tools=[]
        )
        # The run continues with a lossy digest rather than dying.
        assert outcome.compacted
        assert "mechanical digest" in compacted[0].text

    async def test_unsplittable_history_reports_rather_than_corrupting(self) -> None:
        provider = FakeProvider([text_completion("s")], context_window=20_000)
        manager = self._manager(provider, compaction_keep_recent=10)
        history = [Message.user("only one turn")]

        compacted, outcome = await manager.compact(history, system="s", tools=[])
        assert not outcome.compacted
        assert compacted == history
        assert "no safe split point" in outcome.reason

    async def test_impossible_budget_raises_a_clear_error(self) -> None:
        provider = FakeProvider([text_completion("s")], context_window=5_000)
        manager = self._manager(provider, compaction_keep_recent=4)
        # Recent turns alone are far larger than the whole window.
        history = _long_history(turns=6, size=20_000)

        with pytest.raises(ContextOverflowError, match="(?i)even after compaction"):
            await manager.compact(history, system="s", tools=[])


class TestSystemPrompt:
    def test_contains_the_load_bearing_sections(self, settings: Settings) -> None:
        from datetime import UTC, datetime

        prompt = build_system_prompt(
            settings,
            now=datetime.now(UTC),
            account={"id": 1, "username": "owner"},
            tool_names=["telegram_list_dialogs", "python"],
        )
        for marker in (
            "Trust and safety",
            "never instructions",
            "Permissions",
            "large histories",
            "telegram_api_search",
        ):
            assert marker.lower() in prompt.lower(), marker

    def test_read_only_mode_is_announced(self, settings: Settings) -> None:
        from datetime import UTC, datetime

        settings.permissions.read_only_mode = True
        prompt = build_system_prompt(settings, now=datetime.now(UTC))
        assert "READ-ONLY MODE" in prompt

    def test_the_prompt_is_built_only_from_code_and_host_state(
        self, settings: Settings
    ) -> None:
        # Nothing from Telegram may influence the system prompt; the only
        # variable inputs are host-controlled.
        from datetime import UTC, datetime

        a = build_system_prompt(settings, now=datetime(2026, 1, 1, tzinfo=UTC))
        b = build_system_prompt(settings, now=datetime(2026, 1, 1, tzinfo=UTC))
        assert a == b


class TestRedaction:
    def test_registered_secrets_are_replaced(self) -> None:
        registry = SecretRegistry()
        registry.register("my-super-secret-hash-value")
        assert (
            redact_text("connecting with my-super-secret-hash-value now", registry=registry)
            == f"connecting with {PLACEHOLDER} now"
        )

    def test_short_values_are_not_registered(self) -> None:
        # Registering "ab" would mangle ordinary prose.
        registry = SecretRegistry()
        registry.register("ab")
        assert registry.values == frozenset()

    @pytest.mark.parametrize(
        "text",
        [
            "token is 1234567890:AAHqwertyuiopasdfghjklzxcvbnm1234567",
            "key sk-ant-api03-abcdefghijklmnopqrstuvwx",
            "OPENAI key sk-proj-abcdefghijklmnopqrstuvwxyz",
            "api_hash=0123456789abcdef0123456789abcdef",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef",
            "proxy socks5://user:hunter2@host:1080",
        ],
    )
    def test_credential_shapes_are_caught_without_registration(self, text: str) -> None:
        assert PLACEHOLDER in redact_text(text)

    def test_ordinary_text_is_untouched(self) -> None:
        text = "Read 42 messages from @alex between January 1 and January 31."
        assert redact_text(text) == text

    def test_secret_looking_keys_are_blanked(self) -> None:
        payload = redact_value({"api_hash": "0" * 32, "chat": "@alex"})
        assert payload["api_hash"] == PLACEHOLDER
        assert payload["chat"] == "@alex"

    def test_numeric_fields_are_never_blanked_by_key(self) -> None:
        # `tokens` contains "token" but a count is not a credential; blanking it
        # would destroy the observability the logs exist for.
        payload = redact_value(
            {"tokens": 1234, "input_tokens": 900, "max_tokens": 8192, "auth_token": "abcdef123456"}
        )
        assert payload["tokens"] == 1234
        assert payload["input_tokens"] == 900
        assert payload["max_tokens"] == 8192
        assert payload["auth_token"] == PLACEHOLDER

    def test_nested_structures_are_walked(self) -> None:
        payload = redact_value(
            {"outer": {"inner": ["prefix sk-ant-api03-abcdefghijklmnopqrst suffix"]}}
        )
        assert PLACEHOLDER in str(payload)

    def test_recursion_is_bounded(self) -> None:
        node: dict = {"leaf": "sk-ant-api03-abcdefghijklmnopqrst"}
        for _ in range(40):
            node = {"child": node}
        redact_value(node)  # must return rather than hang or overflow
