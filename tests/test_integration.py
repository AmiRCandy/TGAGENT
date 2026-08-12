"""End-to-end flows across subsystems — still entirely offline.

These are the tests that would catch a wiring mistake no unit test can see: the
agent asking for a tool, the tool reaching the gateway, the gateway consulting
policy, the sandbox marshalling over RPC, and the result coming back fenced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.fakes import (
    CollectingEvents,
    FakeClientManager,
    FakeMessage,
    FakeTelegramClient,
    RecordingConfirmation,
)
from tgagent.agent.runtime import AgentRuntime, RuntimeDependencies
from tgagent.config.settings import Settings
from tgagent.llm.providers.fake import FakeProvider, text_completion, tool_call_completion
from tgagent.risk import PolicyDecision
from tgagent.sandbox import create_sandbox
from tgagent.security.permissions import PermissionEngine
from tgagent.storage.sqlite import SQLiteStorage
from tgagent.telegram.gateway import TelegramGateway
from tgagent.telegram.history import HistoryReader
from tgagent.telegram.schema import TelegramSchemaIndex
from tgagent.tools import build_default_registry

pytestmark = pytest.mark.integration


def _runtime(
    provider: FakeProvider,
    settings: Settings,
    gateway: TelegramGateway,
    storage: SQLiteStorage,
    *,
    sandbox: Any = None,
    schema: Any = None,
) -> AgentRuntime:
    return AgentRuntime(
        provider,
        build_default_registry(settings),
        settings,
        RuntimeDependencies(
            gateway=gateway,
            history=HistoryReader(gateway),
            schema=schema,
            sandbox=sandbox,
            memory=storage.memory,
            tasks=storage.tasks,
            conversations=storage.conversations,
            permissions=PermissionEngine(settings.permissions),
            account={"id": 1, "username": "owner"},
        ),
    )


class TestReadFlow:
    async def test_search_then_summarise(
        self,
        settings: Settings,
        gateway: TelegramGateway,
        storage: SQLiteStorage,
        fake_client: FakeTelegramClient,
    ) -> None:
        fake_client.messages = [
            FakeMessage(1, "the VPS migration starts Monday"),
            FakeMessage(2, "unrelated chatter"),
            FakeMessage(3, "migration finished, all green"),
        ]
        provider = FakeProvider(
            [
                tool_call_completion(
                    "telegram_search_messages", {"query": "migration", "peer": "@alex"}
                ),
                text_completion("Two messages mention the migration: it started Monday and finished."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run(
            "what did Alex say about the migration?"
        )

        assert result.succeeded
        assert "migration" in result.answer.lower()
        assert result.tool_calls == 1

    async def test_dialogs_then_history(
        self, settings: Settings, gateway: TelegramGateway, storage: SQLiteStorage
    ) -> None:
        provider = FakeProvider(
            [
                tool_call_completion("telegram_list_dialogs", {"limit": 5}),
                tool_call_completion(
                    "telegram_read_history", {"peer": "@alex", "limit": 10}
                ),
                text_completion("Read both."),
            ]
        )
        events = CollectingEvents()
        result = await _runtime(provider, settings, gateway, storage).run(
            "catch me up", on_event=events
        )

        assert result.tool_calls == 2
        tools_used = [
            e.data["tool"] for e in events.events if e.data.get("tool") and "ok" in e.data
        ]
        assert tools_used == ["telegram_list_dialogs", "telegram_read_history"]


class TestWriteFlow:
    async def test_send_requires_and_receives_confirmation(
        self,
        settings: Settings,
        gateway: TelegramGateway,
        storage: SQLiteStorage,
        confirmations: RecordingConfirmation,
        fake_client: FakeTelegramClient,
    ) -> None:
        provider = FakeProvider(
            [
                tool_call_completion(
                    "telegram_send_message", {"peer": "@alex", "message": "on my way"}
                ),
                text_completion("Sent."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run(
            "tell Alex I'm on my way"
        )

        assert result.succeeded
        assert len(confirmations.requests) == 1
        assert fake_client.sent == [{"entity": "@alex", "message": "on my way"}]

    async def test_declined_send_is_reported_to_the_model_not_crashed(
        self,
        settings: Settings,
        manager: FakeClientManager,
        storage: SQLiteStorage,
    ) -> None:
        declining = RecordingConfirmation(approve=False)
        gateway = TelegramGateway(
            manager,  # type: ignore[arg-type]
            permissions=PermissionEngine(settings.permissions),
            confirmations=declining,
            audit=storage.audit,
            permission_settings=settings.permissions,
        )
        provider = FakeProvider(
            [
                tool_call_completion(
                    "telegram_send_message", {"peer": "@alex", "message": "hi"}
                ),
                text_completion("You declined, so I did not send it."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run("message Alex")

        assert result.succeeded
        assert "declined" in result.answer.lower()
        assert manager.client.sent == []

    async def test_unattended_run_cannot_send(
        self, settings: Settings, gateway: TelegramGateway, storage: SQLiteStorage,
        fake_client: FakeTelegramClient,
    ) -> None:
        provider = FakeProvider(
            [
                tool_call_completion(
                    "telegram_send_message", {"peer": "@alex", "message": "auto"}
                ),
                text_completion("I could not send it: no one was available to confirm."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run(
            "send the daily summary", interactive=False
        )

        assert fake_client.sent == []
        assert result.succeeded  # the run completes; it just reports the refusal


class TestSandboxFlow:
    @pytest.mark.slow
    async def test_generated_code_reaches_telegram_through_the_gateway(
        self,
        settings: Settings,
        gateway: TelegramGateway,
        storage: SQLiteStorage,
        fake_client: FakeTelegramClient,
    ) -> None:
        fake_client.messages = [
            FakeMessage(i, f"line {i} about project x" if i % 3 == 0 else f"noise {i}")
            for i in range(1, 31)
        ]
        code = (
            "msgs = tg.get_messages(entity='@alex', limit=30)\n"
            "hits = [m for m in msgs if 'project x' in (m.get('text') or '').lower()]\n"
            "print(f'scanned {len(msgs)} matched {len(hits)}')\n"
            "result = [m['id'] for m in hits]\n"
        )
        provider = FakeProvider(
            [
                tool_call_completion("python", {"code": code, "purpose": "filter for project x"}),
                text_completion("Found the project X messages."),
            ]
        )
        sandbox = create_sandbox(settings.sandbox)
        result = await _runtime(
            provider, settings, gateway, storage, sandbox=sandbox
        ).run("find project X mentions from Alex")

        assert result.succeeded
        # The tool result the model saw must contain the program's output.
        from tgagent.llm.base import ToolResultPart

        tool_results = [
            p
            for m in provider.requests[1].messages
            for p in m.content
            if isinstance(p, ToolResultPart)
        ]
        assert "scanned 30 matched" in tool_results[0].content
        await sandbox.close()

    @pytest.mark.slow
    async def test_policy_applies_inside_generated_code(
        self,
        settings: Settings,
        manager: FakeClientManager,
        storage: SQLiteStorage,
    ) -> None:
        # The whole point of the RPC design: code cannot route around policy.
        declining = RecordingConfirmation(approve=False)
        gateway = TelegramGateway(
            manager,  # type: ignore[arg-type]
            permissions=PermissionEngine(settings.permissions),
            confirmations=declining,
            audit=storage.audit,
            permission_settings=settings.permissions,
        )
        code = (
            "try:\n"
            "    tg.send_message(entity='@victim', message='pwned')\n"
            "    result = 'sent'\n"
            "except PermissionDeniedError as exc:\n"
            "    result = 'blocked'\n"
        )
        provider = FakeProvider(
            [
                tool_call_completion("python", {"code": code}),
                text_completion("The send was blocked by policy."),
            ]
        )
        sandbox = create_sandbox(settings.sandbox)
        await _runtime(provider, settings, gateway, storage, sandbox=sandbox).run("go")

        assert manager.client.sent == []
        assert len(declining.requests) == 1
        await sandbox.close()

    @pytest.mark.slow
    async def test_account_security_calls_are_denied_inside_the_sandbox(
        self, settings: Settings, gateway: TelegramGateway, storage: SQLiteStorage
    ) -> None:
        code = (
            "try:\n"
            "    tg.invoke_raw('auth.LogOut', {})\n"
            "    result = 'logged out'\n"
            "except PermissionDeniedError:\n"
            "    result = 'denied'\n"
        )
        provider = FakeProvider(
            [tool_call_completion("python", {"code": code}), text_completion("Denied.")]
        )
        sandbox = create_sandbox(settings.sandbox)
        await _runtime(provider, settings, gateway, storage, sandbox=sandbox).run("go")

        entries = await storage.audit.list_recent()
        logout = [e for e in entries if e.method == "auth.LogOut"]
        assert logout and logout[0].decision == PolicyDecision.DENY.value
        assert logout[0].origin == "sandbox"
        await sandbox.close()


class TestDiscoveryFlow:
    async def test_agent_can_look_up_an_api_then_call_it(
        self,
        settings: Settings,
        gateway: TelegramGateway,
        storage: SQLiteStorage,
        fake_client: FakeTelegramClient,
        tmp_path: Any,
    ) -> None:
        # Tier 2 → tier 3: discover the method, then invoke it.
        schema = TelegramSchemaIndex(tmp_path / "schema.json")
        provider = FakeProvider(
            [
                tool_call_completion(
                    "telegram_api_search", {"query": "get full channel information"}
                ),
                tool_call_completion(
                    "telegram_invoke",
                    {"method": "channels.GetFullChannel", "params": {"channel": "@projectx"}},
                ),
                text_completion("Fetched the channel details."),
            ]
        )
        result = await _runtime(
            provider, settings, gateway, storage, schema=schema
        ).run("what are the details of the Project X channel?")

        assert result.succeeded
        assert any("GetFullChannel" in type(r).__name__ for r in fake_client.raw_calls)


class TestAuditTrail:
    async def test_a_whole_run_is_reconstructible_from_the_audit_log(
        self,
        settings: Settings,
        gateway: TelegramGateway,
        storage: SQLiteStorage,
    ) -> None:
        provider = FakeProvider(
            [
                tool_call_completion("telegram_list_dialogs", {"limit": 3}),
                tool_call_completion(
                    "telegram_send_message", {"peer": "@alex", "message": "hi"}
                ),
                text_completion("Done."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run("greet Alex")

        entries = await storage.audit.list_recent(run_id=result.run_id)
        methods = {e.method for e in entries}
        assert "get_dialogs" in methods
        assert "send_message" in methods
        assert all(e.run_id == result.run_id for e in entries)
        # Message text is never stored; only a digest.
        assert all(e.argument_preview is None for e in entries)


class TestMemoryFlow:
    async def test_a_fact_written_in_one_run_is_read_in_the_next(
        self, settings: Settings, gateway: TelegramGateway, storage: SQLiteStorage
    ) -> None:
        writer = FakeProvider(
            [
                tool_call_completion(
                    "memory_write", {"key": "user.timezone", "value": "Europe/London"}
                ),
                text_completion("Noted."),
            ]
        )
        await _runtime(writer, settings, gateway, storage).run("I'm in London")

        reader = FakeProvider(
            [
                tool_call_completion("memory_read", {"key": "user.timezone"}),
                text_completion("You are in Europe/London."),
            ]
        )
        result = await _runtime(reader, settings, gateway, storage).run("what timezone am I in?")

        from tgagent.llm.base import ToolResultPart

        tool_results = [
            p
            for m in reader.requests[1].messages
            for p in m.content
            if isinstance(p, ToolResultPart)
        ]
        assert "Europe/London" in tool_results[0].content
        assert result.succeeded


class TestSchedulingFlow:
    async def test_agent_can_schedule_a_task_that_persists(
        self, settings: Settings, gateway: TelegramGateway, storage: SQLiteStorage
    ) -> None:
        provider = FakeProvider(
            [
                tool_call_completion(
                    "schedule_create",
                    {
                        "name": "morning-review",
                        "prompt": "Review unread messages and flag anything needing a reply.",
                        "kind": "cron",
                        "expression": "0 8 * * *",
                        "timezone": "UTC",
                    },
                ),
                text_completion("Scheduled for 08:00 daily."),
            ]
        )
        result = await _runtime(provider, settings, gateway, storage).run(
            "every morning, review my unread messages"
        )

        assert result.succeeded
        task = await storage.tasks.get_by_name("morning-review")
        assert task is not None
        assert task.enabled
        assert task.next_run_at is not None
        assert "unread" in task.prompt
