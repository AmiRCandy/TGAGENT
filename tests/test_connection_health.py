"""Surviving a connection that dies without saying so.

The reported failure: a deployed listener answers for fifteen to thirty minutes,
then goes quiet while its logs still say it is running, and only a restart fixes
it. The cause is a socket dropped by a NAT or conntrack table with no RST — the
client still reports itself connected, ``disconnected`` never resolves, and no
update ever arrives again. Sending keeps working, because a write forces a fresh
connection, which is exactly why the process looks healthy from the inside.

So these tests are about the one signal that can tell the difference: whether
anything has been *heard from* Telegram lately.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tgagent.config.settings import TelegramSettings
from tgagent.telegram.client import TelegramClientManager


class DeadSocketClient:
    """Connected, cheerful, and receiving nothing.

    ``is_connected()`` is True and ``disconnected`` never resolves — the shape of
    the failure that the disconnect watchdog cannot see.
    """

    def __init__(self, *, probe_hangs: bool = True, reconnect_ok: bool = True) -> None:
        self._connected = True
        self.probe_hangs = probe_hangs
        self.reconnect_ok = reconnect_ok
        self.handlers: list[Any] = []
        self.disconnects = 0
        self.connects = 0
        self.caught_up = 0
        self.authorized = True

    def is_connected(self) -> bool:
        return self._connected

    @property
    def disconnected(self) -> asyncio.Future[None]:
        return asyncio.get_running_loop().create_future()  # never resolves

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers.append((callback, event))

    async def get_me(self) -> Any:
        if self.probe_hangs and self._connected:
            await asyncio.sleep(3600)  # a dead socket swallows the request
        return type("Me", (), {"id": 1, "username": "owner"})()

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def connect(self) -> None:
        self.connects += 1
        if not self.reconnect_ok:
            raise OSError("network unreachable")
        self._connected = True
        self.probe_hangs = False  # the rebuilt connection works

    async def disconnect(self) -> None:
        self.disconnects += 1
        self._connected = False

    async def catch_up(self) -> None:
        self.caught_up += 1


def make_manager(client: Any, **overrides: Any) -> TelegramClientManager:
    """A manager wired to *client*, ticking fast enough to test.

    The intervals are assigned after construction rather than passed in: the
    field floors exist to stop a typo becoming a hot loop against Telegram, and
    a test wanting 10ms ticks is not a reason to lower them.
    """
    settings = TelegramSettings(api_id=1, api_hash="0" * 32)
    for field, value in {
        "health_check_interval": 0.01,
        "idle_probe_after": 0.02,
        **overrides,
    }.items():
        setattr(settings, field, value)
    manager = TelegramClientManager(settings, Path("unused.session"))
    manager._client = client
    return manager


class TestTellingTheDifference:
    def test_a_quiet_account_is_not_a_broken_one(self) -> None:
        """Nothing arriving is normal. It is only a symptom next to a failed probe."""
        manager = make_manager(DeadSocketClient())
        manager.note_activity()
        assert manager.idle_seconds < 1
        assert manager.healthy

    def test_silence_past_the_threshold_is_not_called_healthy(self) -> None:
        manager = make_manager(DeadSocketClient(), idle_probe_after=30.0)
        manager._last_seen = 0.0  # never heard from
        assert manager.idle_seconds == 0.0  # unknown, not stale

    async def test_any_update_counts_as_a_sign_of_life(self) -> None:
        """Read receipts and typing notifications prove a connection too, so the
        stamp comes from a raw handler rather than from the bridge's messages.

        Awaited exactly as Telethon awaits it — see the next test for why calling
        it any other way is how a broken handler ships.
        """
        client = DeadSocketClient()
        manager = make_manager(client)
        manager._watch_updates()

        assert client.handlers, "no raw handler registered"
        manager._last_seen = 0.0
        callback = client.handlers[0][0]
        await callback(object())
        assert manager._last_seen > 0

    async def test_the_handler_is_awaitable(self) -> None:
        """The regression this exists for, found in production.

        Telethon does `await callback(event)` for every handler. A plain function
        returns None, `await None` raises TypeError, and the result is one logged
        traceback *per update* — which under systemd is thousands of lines into
        journald and an outage of its own. The first version of this test called
        the handler synchronously and so proved nothing.
        """
        import inspect

        client = DeadSocketClient()
        manager = make_manager(client)
        manager._watch_updates()

        callback = client.handlers[0][0]
        assert inspect.iscoroutinefunction(callback), (
            "the raw update handler must be async: Telethon awaits its return value"
        )
        # And awaiting it has to work, not merely be permitted by the signature.
        assert await callback(object()) is None


class TestEveryHandlerWeRegister:
    """Telethon awaits handlers, so a sync one is a per-update exception.

    Broad on purpose: the bug was in a handler added for monitoring, not in the
    bridge's own, and the next one added will not be either.
    """

    def test_no_synchronous_handler_reaches_telethon(self) -> None:
        import inspect

        client = DeadSocketClient()
        manager = make_manager(client)
        manager._watch_updates()

        offenders = [
            getattr(callback, "__name__", repr(callback))
            for callback, _event in client.handlers
            if not inspect.iscoroutinefunction(callback)
        ]
        assert offenders == [], f"synchronous Telethon handlers: {offenders}"


class TestRecovery:
    async def test_a_hung_probe_forces_a_rebuild(self) -> None:
        """The heart of the fix: the probe has a deadline, because a dead socket
        does not refuse a request — it swallows it."""
        client = DeadSocketClient(probe_hangs=True)
        manager = make_manager(client, timeout=0.05)
        manager.note_activity()
        manager._start_monitor()
        try:
            await asyncio.sleep(0.4)
        finally:
            await manager.stop()

        assert client.disconnects >= 1, "never tore the dead connection down"
        assert client.connects >= 1, "never rebuilt it"

    async def test_a_rebuild_asks_for_what_it_missed(self) -> None:
        client = DeadSocketClient(probe_hangs=True)
        manager = make_manager(client, timeout=0.05)
        manager.note_activity()
        await manager._recover()
        assert client.caught_up == 1

    async def test_a_working_connection_is_left_alone(self) -> None:
        """A probe that answers is the end of it — no reconnect, no churn."""
        client = DeadSocketClient(probe_hangs=False)
        manager = make_manager(client)
        manager.note_activity()
        manager._start_monitor()
        try:
            await asyncio.sleep(0.3)
            # Sampled before stop(), which disconnects on purpose.
            churn = (client.disconnects, client.connects)
        finally:
            await manager.stop()

        assert churn == (0, 0)

    async def test_a_successful_probe_counts_as_activity(self) -> None:
        """Otherwise a genuinely quiet account is probed on every single tick."""
        client = DeadSocketClient(probe_hangs=False)
        manager = make_manager(client)
        manager._last_seen = 0.0
        assert await manager._probe()
        manager.note_activity()
        assert manager.idle_seconds < 1

    async def test_it_gives_up_loudly_rather_than_pretending(self) -> None:
        """A listener that cannot hear anything must not keep the port: `failed`
        is what lets the interface exit so a supervisor replaces it."""
        client = DeadSocketClient(probe_hangs=True, reconnect_ok=False)
        manager = make_manager(client, timeout=0.05, max_recovery_attempts=2)
        manager.note_activity()

        for _ in range(2):
            await manager._recover()

        assert manager.failed.is_set()

    async def test_a_revoked_session_stops_immediately(self) -> None:
        """Retrying cannot fix a session somebody logged out; say so and stop."""
        client = DeadSocketClient(probe_hangs=True)
        client.authorized = False
        manager = make_manager(client)
        await manager._recover()
        assert manager.failed.is_set()

    async def test_stopping_ends_the_monitor(self) -> None:
        client = DeadSocketClient(probe_hangs=False)
        manager = make_manager(client)
        manager._start_monitor()
        await asyncio.sleep(0.05)
        await manager.stop()
        assert manager._monitor is None


class TestSettings:
    def test_the_defaults_are_sane_for_a_vps(self) -> None:
        """Fifteen to thirty minutes is when the flow gets dropped, so a
        five-minute idle probe catches it inside the first quiet stretch."""
        settings = TelegramSettings(api_id=1, api_hash="0" * 32)
        assert settings.health_check_interval <= 60.0
        assert settings.idle_probe_after <= 600.0
        assert settings.max_recovery_attempts >= 3

    def test_probing_cannot_be_configured_into_a_hot_loop(self) -> None:
        """A typo here would hammer Telegram, which rate-limits the account."""
        for field, value in (("health_check_interval", 0.0), ("idle_probe_after", 1.0)):
            with pytest.raises(ValidationError):
                TelegramSettings(api_id=1, api_hash="0" * 32, **{field: value})


class TestOneMessageCannotFloodTheJournal:
    """The amplifier that turned a bug into an outage.

    Under a service manager stdout is a pipe to the journal, so a message
    repeating per-update writes faster than the journal drains — and a blocking
    write from the event loop stalls everything while the process stays up and
    idle. The same build under `nohup` writes to a file and looks fine, which is
    exactly how this hides.

    Driven through `logging` rather than the filter's own method, because the
    first attempt at this used a structlog processor raising `DropEvent` —
    which `ProcessorFormatter` does not catch on the foreign-log path, turning
    every suppressed line into a `--- Logging error ---` traceback. Only an
    end-to-end assertion catches that.
    """

    def _captured(self, emit: Any) -> str:
        import io
        import logging

        from tgagent.config.settings import LoggingSettings
        from tgagent.observability.logging import configure_logging

        configure_logging(LoggingSettings(format="json", level="INFO"))
        buffer = io.StringIO()
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.stream = buffer
        emit()
        return buffer.getvalue()

    def test_a_repeating_third_party_message_is_capped(self) -> None:
        import logging

        def emit() -> None:
            log = logging.getLogger("telethon.client.updates")
            log.setLevel(logging.ERROR)
            for _ in range(200):
                log.error("Unhandled exception on _seen")

        written = [line for line in self._captured(emit).splitlines() if line.strip()]
        assert len(written) < 40, f"{len(written)} lines written for 200 events"
        assert sum("suppressed" in line for line in written) == 1

    def test_suppressing_does_not_itself_become_an_error(self) -> None:
        """The trap: dropping a record the wrong way logs a traceback per drop."""
        import logging

        def emit() -> None:
            log = logging.getLogger("telethon.client.updates")
            log.setLevel(logging.ERROR)
            for _ in range(200):
                log.error("Unhandled exception on _seen")

        assert "--- Logging error ---" not in self._captured(emit)

    def test_a_flood_of_one_thing_does_not_silence_another(self) -> None:
        """The message that finally explains the problem must not be the one that
        gets dropped."""
        import logging

        def emit() -> None:
            noisy = logging.getLogger("telethon.client.updates")
            noisy.setLevel(logging.ERROR)
            for _ in range(200):
                noisy.error("Unhandled exception on _seen")
            logging.getLogger("tgagent.app").info("app.stopped")

        assert "app.stopped" in self._captured(emit)

    def test_distinct_messages_are_never_touched(self) -> None:
        from tgagent.observability.logging import CollapseRepeats

        collapse = CollapseRepeats(limit=5, window=60.0)
        records = [
            logging.LogRecord("tgagent", logging.INFO, __file__, 1, f"run.{i}", None, None)
            for i in range(100)
        ]
        assert all(collapse.filter(record) for record in records)
