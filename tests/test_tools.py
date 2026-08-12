"""The tool layer: registry, schemas, curated Telegram tools, memory, docs."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fakes import FakeTelegramClient, RecordingConfirmation
from tgagent.config.settings import Settings
from tgagent.errors import PermissionDenied, ToolInputError
from tgagent.risk import TrustLevel
from tgagent.storage.sqlite import SQLiteStorage
from tgagent.telegram.schema import TelegramSchemaIndex
from tgagent.tools import build_default_registry
from tgagent.tools.base import ToolContext, ToolRegistry, ToolResult, clamp_int, object_schema
from tgagent.tools.docs_tool import ApiSearchTool
from tgagent.tools.memory_tools import MemoryReadTool, MemoryWriteTool
from tgagent.tools.telegram_tools import (
    DeleteMessagesTool,
    InvokeTool,
    ListDialogsTool,
    ReadHistoryTool,
    ResolvePeerTool,
    SearchMessagesTool,
    SendMessageTool,
    build_telegram_tools,
)


class TestRegistry:
    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        tool = ListDialogsTool()
        registry.register(tool)
        assert registry.get("telegram_list_dialogs") is tool
        assert "telegram_list_dialogs" in registry

    def test_duplicate_registration_refused(self) -> None:
        registry = ToolRegistry()
        registry.register(ListDialogsTool())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(ListDialogsTool())

    def test_unknown_tool_lists_alternatives(self) -> None:
        from tgagent.errors import ToolNotFound

        registry = ToolRegistry()
        registry.register(ListDialogsTool())
        with pytest.raises(ToolNotFound, match="Available tools"):
            registry.get("nope")

    def test_specs_are_stably_ordered(self) -> None:
        # Byte-identical tool arrays between requests are what let provider-side
        # prompt caching work, so ordering must not depend on insertion order.
        first = ToolRegistry()
        first.register_all(build_telegram_tools())
        second = ToolRegistry()
        second.register_all(list(reversed(build_telegram_tools())))
        assert [s.name for s in first.specs()] == [s.name for s in second.specs()]

    def test_every_tool_advertises_a_valid_schema(self) -> None:
        for spec in _all_specs():
            assert spec.name and spec.description
            assert spec.parameters["type"] == "object"
            assert "properties" in spec.parameters
            for name in spec.parameters.get("required", []):
                assert name in spec.parameters["properties"], f"{spec.name}.{name}"

    def test_descriptions_are_substantial(self) -> None:
        # A one-line description is the most common cause of a tool being used
        # wrongly or not at all.
        for spec in _all_specs():
            assert len(spec.description) > 60, spec.name

    def test_destructive_tools_warn_in_their_description(self) -> None:
        description = DeleteMessagesTool().description.lower()
        assert "not reversible" in description or "irreversible" in description

    def test_feature_flags_shape_the_registry(self, settings: Settings) -> None:
        settings.features.code_execution = False
        settings.features.scheduling = False
        settings.features.memory = False
        registry = build_default_registry(settings)
        names = registry.names()
        assert "python" not in names
        assert not any(n.startswith("schedule_") for n in names)
        assert not any(n.startswith("memory_") for n in names)
        assert "telegram_list_dialogs" in names

    def test_disabled_sandbox_removes_the_python_tool(self, settings: Settings) -> None:
        settings.sandbox.backend = "disabled"
        assert "python" not in build_default_registry(settings).names()

    def test_media_flag_removes_the_download_tool(self, settings: Settings) -> None:
        settings.features.media_download = False
        assert "telegram_download_media" not in build_default_registry(settings).names()


class TestReadTools:
    async def test_list_dialogs(self, tool_context: ToolContext) -> None:
        result = await ListDialogsTool().run({"limit": 10}, tool_context)
        payload = json.loads(result.content)
        assert payload["count"] >= 1
        assert result.trust is TrustLevel.UNTRUSTED  # chat titles are user content

    async def test_list_dialogs_only_unread(self, tool_context: ToolContext) -> None:
        result = await ListDialogsTool().run({"only_unread": True}, tool_context)
        payload = json.loads(result.content)
        assert all(d["unread_count"] > 0 for d in payload["dialogs"])

    async def test_read_history_paginates(self, tool_context: ToolContext) -> None:
        result = await ReadHistoryTool().run({"peer": "@alex", "limit": 5}, tool_context)
        payload = json.loads(result.content)
        assert len(payload["messages"]) == 5
        assert payload["has_more"]
        assert "next_offset_id" in payload

    async def test_read_history_requires_a_peer(self, tool_context: ToolContext) -> None:
        with pytest.raises(ToolInputError, match="required"):
            await ReadHistoryTool().run({}, tool_context)

    async def test_limits_are_clamped_not_rejected(self, tool_context: ToolContext) -> None:
        # A model asking for 100000 messages should get a page, not an error.
        result = await ReadHistoryTool().run({"peer": "@alex", "limit": 100_000}, tool_context)
        payload = json.loads(result.content)
        assert len(payload["messages"]) <= 200

    async def test_resolve_peer(self, tool_context: ToolContext) -> None:
        result = await ResolvePeerTool().run({"peer": "@alex"}, tool_context)
        payload = json.loads(result.content)
        assert payload["id"] == 12345
        assert payload["kind"] == "user"

    async def test_search_in_chat(self, tool_context: ToolContext) -> None:
        result = await SearchMessagesTool().run(
            {"query": "message 3", "peer": "@alex"}, tool_context
        )
        assert "messages" in json.loads(result.content)

    async def test_search_rejects_an_unknown_media_filter(self, tool_context: ToolContext) -> None:
        with pytest.raises(ToolInputError, match="Valid values"):
            await SearchMessagesTool().run(
                {"query": "x", "peer": "@a", "media_filter": "hologram"}, tool_context
            )

    async def test_global_search_uses_the_raw_api(
        self, tool_context: ToolContext, fake_client: FakeTelegramClient
    ) -> None:
        await SearchMessagesTool().run({"query": "migration"}, tool_context)
        # There is no friendly global-search method, so this must go through TL.
        assert any("Search" in type(r).__name__ for r in fake_client.raw_calls)


class TestWriteTools:
    async def test_send_message_goes_through_confirmation(
        self,
        tool_context: ToolContext,
        confirmations: RecordingConfirmation,
        fake_client: FakeTelegramClient,
    ) -> None:
        result = await SendMessageTool().run({"peer": "@alex", "message": "hello"}, tool_context)
        assert json.loads(result.content)["sent"] is True
        assert len(confirmations.requests) == 1
        assert fake_client.sent == [{"entity": "@alex", "message": "hello"}]

    async def test_declined_send_raises_permission_denied(
        self, tool_context: ToolContext, confirmations: RecordingConfirmation
    ) -> None:
        confirmations.approve = False
        with pytest.raises(PermissionDenied):
            await SendMessageTool().run({"peer": "@alex", "message": "x"}, tool_context)

    async def test_delete_requires_a_non_empty_id_list(self, tool_context: ToolContext) -> None:
        with pytest.raises(ToolInputError):
            await DeleteMessagesTool().run({"peer": "@alex", "message_ids": []}, tool_context)

    async def test_forward_rejects_more_than_the_api_allows(
        self, tool_context: ToolContext
    ) -> None:
        from tgagent.tools.telegram_tools import ForwardMessagesTool

        with pytest.raises(ToolInputError, match="at most 100"):
            await ForwardMessagesTool().run(
                {"from_peer": "@a", "to_peer": "@b", "message_ids": list(range(101))},
                tool_context,
            )


class TestInvokeTool:
    async def test_raw_call(
        self, tool_context: ToolContext, fake_client: FakeTelegramClient
    ) -> None:
        result = await InvokeTool().run(
            {
                "method": "messages.Search",
                "params": {
                    "peer": "@alex",
                    "q": "x",
                    "filter": {"_": "InputMessagesFilterEmpty"},
                    "min_date": 0,
                    "max_date": 0,
                    "offset_id": 0,
                    "add_offset": 0,
                    "limit": 5,
                    "max_id": 0,
                    "min_id": 0,
                    "hash": 0,
                },
            },
            tool_context,
        )
        assert result.trust is TrustLevel.UNTRUSTED
        assert fake_client.raw_calls

    async def test_params_must_be_an_object(self, tool_context: ToolContext) -> None:
        with pytest.raises(ToolInputError, match="JSON object"):
            await InvokeTool().run({"method": "get_me", "params": "nope"}, tool_context)

    async def test_policy_still_applies_to_raw_calls(self, tool_context: ToolContext) -> None:
        # The generic escape hatch must not bypass classification.
        with pytest.raises(PermissionDenied):
            await InvokeTool().run({"method": "auth.LogOut", "params": {}}, tool_context)


class TestApiSearchTool:
    async def test_finds_a_known_method(self, tool_context: ToolContext, tmp_path: Any) -> None:
        tool_context.schema = TelegramSchemaIndex(tmp_path / "schema.json")
        result = await ApiSearchTool().run({"query": "messages.Search"}, tool_context)
        assert "messages.Search" in result.content
        assert "invoke_raw" in result.content

    async def test_finds_by_intent(self, tool_context: ToolContext, tmp_path: Any) -> None:
        tool_context.schema = TelegramSchemaIndex(tmp_path / "schema.json")
        result = await ApiSearchTool().run({"query": "download a file"}, tool_context)
        assert "download" in result.content.lower()

    async def test_no_match_suggests_namespaces(
        self, tool_context: ToolContext, tmp_path: Any
    ) -> None:
        tool_context.schema = TelegramSchemaIndex(tmp_path / "schema.json")
        result = await ApiSearchTool().run({"query": "zzzzqqqqxxxx"}, tool_context)
        assert "No API method matched" in result.content

    async def test_missing_index_is_reported(self, tool_context: ToolContext) -> None:
        tool_context.schema = None
        result = await ApiSearchTool().run({"query": "x"}, tool_context)
        assert result.is_error


class TestMemoryTools:
    async def test_write_then_read(self, tool_context: ToolContext, storage: SQLiteStorage) -> None:
        tool_context.memory = storage.memory
        await MemoryWriteTool().run(
            {"key": "user.timezone", "value": "Europe/London"}, tool_context
        )
        result = await MemoryReadTool().run({"key": "user.timezone"}, tool_context)
        payload = json.loads(result.content)
        assert payload["facts"][0]["value"] == "Europe/London"

    async def test_rewriting_a_key_replaces_it(
        self, tool_context: ToolContext, storage: SQLiteStorage
    ) -> None:
        tool_context.memory = storage.memory
        await MemoryWriteTool().run({"key": "k", "value": "old"}, tool_context)
        result = await MemoryWriteTool().run({"key": "k", "value": "new"}, tool_context)
        assert json.loads(result.content)["replaced"] is True

    async def test_recalled_facts_are_untrusted(
        self, tool_context: ToolContext, storage: SQLiteStorage
    ) -> None:
        # A remembered fact may quote a Telegram message, so it is data.
        tool_context.memory = storage.memory
        await MemoryWriteTool().run({"key": "k", "value": "v"}, tool_context)
        result = await MemoryReadTool().run({"query": "k"}, tool_context)
        assert result.trust is TrustLevel.UNTRUSTED

    async def test_disabled_memory_reports_cleanly(self, tool_context: ToolContext) -> None:
        tool_context.settings.features.memory = False
        result = await MemoryReadTool().run({"key": "x"}, tool_context)
        assert result.is_error
        assert "disabled" in result.content


class TestHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5), (0, 1), (10_000, 100), ("7", 7), (None, 50), ("nonsense", 50)],
    )
    def test_clamp_int(self, value: Any, expected: int) -> None:
        assert clamp_int(value, default=50, minimum=1, maximum=100) == expected

    def test_object_schema_shape(self) -> None:
        schema = object_schema({"a": {"type": "string"}}, required=["a"])
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["a"]

    def test_tool_result_constructors(self) -> None:
        assert ToolResult.error("bad").is_error
        untrusted = ToolResult.untrusted("data", source="telegram:x")
        assert untrusted.trust is TrustLevel.UNTRUSTED
        assert untrusted.source == "telegram:x"


def _all_specs() -> list[Any]:
    registry = ToolRegistry()
    registry.register_all(build_telegram_tools())
    registry.register(ApiSearchTool())
    from tgagent.tools.code_tool import PythonTool
    from tgagent.tools.memory_tools import build_memory_tools
    from tgagent.tools.schedule_tools import build_schedule_tools

    registry.register(PythonTool())
    registry.register_all(build_memory_tools())
    registry.register_all(build_schedule_tools())
    return registry.specs()
