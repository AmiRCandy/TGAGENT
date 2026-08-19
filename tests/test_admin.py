"""Changing policy and model settings from a chat.

This is the most privileged surface in the project — one message can widen what
the account is allowed to do, or point the model at a different endpoint — so the
tests that matter are about who can reach it and what it refuses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeClientManager, FakeControlEvent, FakeEntity
from tgagent.agent.events import RunResult
from tgagent.config.local import load_local_overrides, local_overrides_path
from tgagent.config.policy import chat_policy_path, load_chat_overrides, resolve_permissions
from tgagent.config.settings import (
    AutoReplySettings,
    PermissionSettings,
    Settings,
    TelegramControlSettings,
)
from tgagent.interfaces.admin import RuntimeAdmin
from tgagent.interfaces.autoreply import ANY_PRIVATE_CHAT, AutoReplyWatcher, IncomingMessage
from tgagent.interfaces.telegram_control import TelegramControlBridge
from tgagent.risk import PolicyDecision, RiskTier
from tgagent.security.permissions import PermissionEngine
from tgagent.storage.sqlite import SQLiteStorage

OWNER_ID = 1
STRANGER_ID = 77


# --------------------------------------------------------------- doubles ------
class QuietRuntime:
    """A runtime that must never be reached: these commands do not use a model."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, prompt: str, **_kwargs: Any) -> RunResult:
        self.prompts.append(prompt)
        return RunResult(run_id="r", conversation_id="c", answer="should not happen")


@pytest.fixture
def admin_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path,
        telegram={"api_id": 1, "api_hash": "0" * 32},
        llm={"provider": "fake", "model": "fake-model"},
        permissions=PermissionSettings(),
    )
    settings.ensure_directories()
    return settings


@pytest.fixture
def engine(admin_settings: Settings) -> PermissionEngine:
    return PermissionEngine(admin_settings.permissions)


@pytest.fixture
def admin(admin_settings: Settings, engine: PermissionEngine) -> RuntimeAdmin:
    return RuntimeAdmin(admin_settings, engine)


def make_bridge(
    manager: FakeClientManager,
    admin: RuntimeAdmin | None,
    *,
    watcher: AutoReplyWatcher | None = None,
    runtime: Any = None,
) -> TelegramControlBridge:
    return TelegramControlBridge(
        manager,
        lambda: runtime or QuietRuntime(),
        TelegramControlSettings(progress_updates=False),
        me_id=OWNER_ID,
        watcher=watcher,
        admin=admin,
    )


def sent(manager: FakeClientManager) -> list[str]:
    return [entry["message"] for entry in manager.client.sent]


# ---------------------------------------------------------------- policy ------
class TestPolicyCommand:
    def test_it_reports_the_policy_without_a_model(self, admin: RuntimeAdmin) -> None:
        message = admin.policy("").message
        assert "externally_visible" in message
        assert "account_security" in message

    def test_add_and_allow_mean_the_same_thing(
        self, admin: RuntimeAdmin, engine: PermissionEngine
    ) -> None:
        """`policy add send_message` is what somebody types; it has to work."""
        result = admin.policy("add send_message")
        assert result.changed
        assert engine.explain("messages.SendMessage").decision is PolicyDecision.ALLOW

    def test_a_change_takes_effect_in_this_process(
        self, admin: RuntimeAdmin, engine: PermissionEngine
    ) -> None:
        """A policy change needing a restart would be useless from a phone."""
        # A destructive method, so the assertion holds whatever this deployment's
        # baseline for externally-visible operations happens to be.
        assert engine.explain("messages.DeleteHistory").decision is not PolicyDecision.ALLOW
        admin.policy("allow messages.DeleteHistory")
        assert engine.explain("messages.DeleteHistory").decision is PolicyDecision.ALLOW

    def test_a_change_survives_a_restart(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        admin.policy("allow send_message")

        # What the next process will do: resolve the policy from disk.
        resolved = resolve_permissions(PermissionSettings(), data_dir=admin_settings.data_dir)
        assert PermissionEngine(resolved).explain("send_message").decision is PolicyDecision.ALLOW

    def test_it_writes_a_separate_file_from_the_operators_own_policy(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        """Their file keeps its comments; everything set remotely is in one place."""
        admin.policy("allow send_message")
        path = chat_policy_path(admin_settings.data_dir)
        assert path.exists()
        assert "send_message" in load_chat_overrides(admin_settings.data_dir)
        assert "written by" in path.read_text(encoding="utf-8").lower()

    def test_reset_puts_a_method_back(self, admin: RuntimeAdmin, engine: PermissionEngine) -> None:
        # Compared against the tier default rather than a literal: what "back"
        # means is a deployment's own baseline, and this test is about `remove`.
        default = engine.settings.defaults[RiskTier.EXTERNALLY_VISIBLE]
        admin.policy("deny send_message")
        result = admin.policy("remove send_message")
        assert result.changed
        assert engine.explain("send_message").decision is default

    def test_one_method_can_be_queried(self, admin: RuntimeAdmin) -> None:
        message = admin.policy("messages.DeleteHistory").message
        assert "destructive" in message

    @pytest.mark.parametrize(
        "method",
        ["account.DeleteAccount", "auth.LogOut", "account.UpdatePasswordSettings"],
    )
    def test_it_cannot_open_the_account_itself(self, admin: RuntimeAdmin, method: str) -> None:
        """A stolen phone must not be able to grant "reset my sessions"."""
        result = admin.policy(f"allow {method}")
        assert not result.changed
        assert "policy.yaml" in result.message

    def test_it_can_always_tighten(self, admin: RuntimeAdmin, engine: PermissionEngine) -> None:
        """Denying is safe by construction, including for the account-security set."""
        result = admin.policy("deny auth.LogOut")
        assert result.changed
        assert engine.explain("auth.LogOut").decision is PolicyDecision.DENY

    def test_it_refuses_to_allow_a_method_that_does_not_exist(self, admin: RuntimeAdmin) -> None:
        """An `allow` on a typo is a permission the operator thinks they granted."""
        result = admin.policy("allow sendmesage")
        assert not result.changed
        assert "not a Telegram method" in result.message

    def test_it_explains_itself_when_it_cannot_parse(self, admin: RuntimeAdmin) -> None:
        message = admin.policy("please be nicer to alex").message
        assert "policy allow" in message


# ------------------------------------------------------------------- llm ------
class TestLlmCommand:
    def test_it_reports_the_model(self, admin: RuntimeAdmin) -> None:
        assert "fake-model" in admin.llm("").message

    def test_the_model_can_be_changed(self, admin: RuntimeAdmin, admin_settings: Settings) -> None:
        reloaded: list[bool] = []
        admin = RuntimeAdmin(
            admin_settings,
            PermissionEngine(admin_settings.permissions),
            on_llm_changed=lambda: reloaded.append(True),
        )
        result = admin.llm("model claude-opus-5")

        assert result.changed
        assert admin_settings.llm.model == "claude-opus-5"
        # The provider is cached, so a change that does not drop it is a lie.
        assert reloaded == [True]

    def test_a_change_survives_a_restart(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        admin.llm("model claude-sonnet-5")
        stored = load_local_overrides(admin_settings.data_dir)
        assert stored["llm.model"] == "claude-sonnet-5"

    def test_an_api_key_is_never_echoed_back(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        secret = "sk-ant-0123456789abcdef"
        result = admin.llm(f"key {secret}")

        assert result.changed
        assert secret not in result.message
        assert result.contained_secret is True
        assert admin_settings.llm.api_key is not None
        assert admin_settings.llm.api_key.get_secret_value() == secret

    def test_the_file_holding_a_key_is_owner_only(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        admin.llm("key sk-ant-0123456789abcdef")
        path = local_overrides_path(admin_settings.data_dir)
        assert path.exists()
        # Owner-only is best effort by design: Windows does not honour the mode, so
        # the assertion only holds where modes are real.
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0

    def test_a_base_url_must_be_a_url(self, admin: RuntimeAdmin) -> None:
        result = admin.llm("url my-server:8080")
        assert not result.changed
        assert "http" in result.message

    def test_a_base_url_change_warns_about_what_it_means(self, admin: RuntimeAdmin) -> None:
        """Every message the agent processes would go to that endpoint."""
        result = admin.llm("url https://gateway.example.com/v1")
        assert result.changed
        assert "endpoint" in result.message.lower()

    def test_reset_restores_the_environment(
        self, admin: RuntimeAdmin, admin_settings: Settings
    ) -> None:
        admin.llm("model claude-opus-5")
        result = admin.llm("reset model")
        assert result.changed
        assert "llm.model" not in load_local_overrides(admin_settings.data_dir)

    def test_an_unknown_setting_gets_the_usage(self, admin: RuntimeAdmin) -> None:
        assert "llm model" in admin.llm("temperature 0.9").message


# ------------------------------------------------------------ the gate --------
class TestOnlyTheOwner:
    async def test_a_stranger_cannot_change_the_policy(
        self, manager: FakeClientManager, admin: RuntimeAdmin, engine: PermissionEngine
    ) -> None:
        """allowed_senders can spend your tokens. It does not extend to this."""
        bridge = make_bridge(manager, admin)
        event = FakeControlEvent(
            "agent policy allow send_message",
            sender_id=STRANGER_ID,
            out=False,
            sender=FakeEntity(STRANGER_ID, username="mallory"),
        )
        # Reach _administer directly: authorisation for *commands* would already
        # have refused this sender, so this pins the second, stricter check.
        before = engine.explain("send_message").decision
        source = await bridge._describe(event, event.chat_id, event.id, "policy allow send_message")
        await bridge._administer(source, "policy", "allow send_message")

        assert engine.explain("send_message").decision is before
        assert "owner" in sent(manager)[0]

    async def test_the_owner_can(
        self, manager: FakeClientManager, admin: RuntimeAdmin, engine: PermissionEngine
    ) -> None:
        bridge = make_bridge(manager, admin)
        assert await bridge.handle_event(
            FakeControlEvent("agent policy allow messages.DeleteHistory", out=True)
        )
        assert engine.explain("messages.DeleteHistory").decision is PolicyDecision.ALLOW

    async def test_a_key_is_deleted_from_the_chat(
        self, manager: FakeClientManager, admin: RuntimeAdmin
    ) -> None:
        """An API key pasted into a chat stays in the history until removed."""
        bridge = make_bridge(manager, admin)
        await bridge.handle_event(
            FakeControlEvent("agent llm key sk-ant-0123456789abcdef", message_id=4242, out=True)
        )

        deletions = [args for name, args in manager.client.calls if name == "delete_messages"]
        assert deletions and 4242 in deletions[0]["message_ids"]
        assert "sk-ant-0123456789abcdef" not in " ".join(sent(manager))

    async def test_it_refuses_while_a_run_is_in_flight(
        self, manager: FakeClientManager, admin: RuntimeAdmin, engine: PermissionEngine
    ) -> None:
        """A run must not observe its own rules changing underneath it."""
        import asyncio

        from tgagent.interfaces.telegram_control import _ActiveRun

        bridge = make_bridge(manager, admin)
        before = engine.explain("send_message").decision
        task = asyncio.create_task(asyncio.sleep(0))
        bridge._active["-100123"] = _ActiveRun(task=task, cancel=asyncio.Event())

        await bridge.handle_event(FakeControlEvent("agent policy allow send_message", out=True))
        await task

        assert engine.explain("send_message").decision is before
        assert "still running" in sent(manager)[0]

    async def test_the_model_is_never_consulted(
        self, manager: FakeClientManager, admin: RuntimeAdmin
    ) -> None:
        runtime = QuietRuntime()
        bridge = make_bridge(manager, admin, runtime=runtime)
        for command in ("agent policy", "agent llm", "agent policy allow send_message"):
            await bridge.handle_event(FakeControlEvent(command, out=True))
        assert runtime.prompts == []


# ------------------------------------------------------------ flight mode -----
class TestFlightMode:
    def _watcher(self, storage: SQLiteStorage) -> AutoReplyWatcher:
        return AutoReplyWatcher(
            storage.watches,
            AutoReplySettings(enabled=True, cooldown_seconds=0.0),
            me_id=OWNER_ID,
        )

    async def test_one_command_answers_every_private_chat(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        """The situation the feature exists for: thirty seconds before boarding."""
        watcher = self._watcher(storage)
        bridge = make_bridge(manager, None, watcher=watcher)

        assert await bridge.handle_event(FakeControlEvent("agent flight on", out=True))
        assert "Flight mode on" in sent(manager)[0]

        watch = await watcher.flight_mode()
        assert watch is not None
        assert watch.chat_id == ANY_PRIVATE_CHAT
        assert watch.expires_at is not None  # bounded like any other watch

    async def test_it_answers_a_chat_it_was_never_told_about(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        watcher = self._watcher(storage)
        await watcher.start_flight_mode(hours=1)

        matched = await watcher.match(
            IncomingMessage(
                chat_id=-987654,
                message_id=5,
                text="are you around?",
                sender_id=STRANGER_ID,
                chat_kind="private chat",
            )
        )
        assert matched is not None

    async def test_it_leaves_groups_alone(self, storage: SQLiteStorage) -> None:
        """Answering for somebody in a group nobody addressed them in is worse."""
        watcher = self._watcher(storage)
        await watcher.start_flight_mode(hours=1)

        matched = await watcher.match(
            IncomingMessage(
                chat_id=-100999,
                message_id=5,
                text="anyone got the deploy key?",
                sender_id=STRANGER_ID,
                chat_kind="group",
            )
        )
        assert matched is None

    async def test_a_chats_own_instruction_beats_the_blanket_one(
        self, storage: SQLiteStorage
    ) -> None:
        from tests.test_autoreply import make_watch

        watcher = self._watcher(storage)
        await watcher.start_flight_mode(hours=1)
        await storage.watches.create(make_watch(chat_id=555, instruction="specific to Alex"))

        matched = await watcher.match(
            IncomingMessage(
                chat_id=555,
                message_id=6,
                text="hi",
                sender_id=STRANGER_ID,
                chat_kind="private chat",
            )
        )
        assert matched is not None
        assert matched.instruction == "specific to Alex"

    async def test_hours_can_be_given(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        watcher = self._watcher(storage)
        bridge = make_bridge(manager, None, watcher=watcher)

        await bridge.handle_event(FakeControlEvent("agent flight on 2", out=True))
        watch = await watcher.flight_mode()
        assert watch is not None and watch.expires_at is not None

        from datetime import UTC, datetime, timedelta

        remaining = watch.expires_at - datetime.now(UTC)
        assert timedelta(minutes=110) < remaining <= timedelta(hours=2)

    async def test_an_instruction_can_be_given_with_the_hours(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        watcher = self._watcher(storage)
        bridge = make_bridge(manager, None, watcher=watcher)

        await bridge.handle_event(
            FakeControlEvent("agent flight on 3 tell them I land at six", out=True)
        )
        watch = await watcher.flight_mode()
        assert watch is not None
        assert "land at six" in watch.instruction

    async def test_landing_stops_it(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        watcher = self._watcher(storage)
        bridge = make_bridge(manager, None, watcher=watcher)
        await watcher.start_flight_mode(hours=1)

        await bridge.handle_event(FakeControlEvent("agent flight off", out=True))
        assert "Flight mode off" in sent(manager)[-1]
        assert await watcher.flight_mode() is None

    async def test_status_says_whether_it_is_on(
        self, manager: FakeClientManager, storage: SQLiteStorage
    ) -> None:
        watcher = self._watcher(storage)
        bridge = make_bridge(manager, None, watcher=watcher)

        await bridge.handle_event(FakeControlEvent("agent flight", out=True))
        assert "off" in sent(manager)[0]

        await watcher.start_flight_mode(hours=1)
        await bridge.handle_event(FakeControlEvent("agent flight", out=True))
        assert "is on" in sent(manager)[-1]

    async def test_it_says_so_when_autoreply_is_switched_off(
        self, manager: FakeClientManager
    ) -> None:
        bridge = make_bridge(manager, None, watcher=None)
        await bridge.handle_event(FakeControlEvent("agent flight on", out=True))
        assert "TGAGENT_AUTOREPLY__ENABLED" in sent(manager)[0]
