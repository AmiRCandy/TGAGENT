"""Telegram client lifecycle.

Owns the one and only :class:`telethon.TelegramClient` instance: construction,
connection, authorisation checks, reconnection, and shutdown. Nothing else in
the project constructs a client, and only :mod:`tgagent.telegram.gateway` is
supposed to call methods on it.

Reconnection
------------
Telethon reconnects automatically for transient drops, driven by
``connection_retries``. What it does not do is tell the application when it has
given up. The watchdog here waits on ``client.disconnected`` and re-establishes
the connection with capped exponential backoff, so a laptop closing its lid or a
network changing underneath a long-running scheduled task does not silently
strand the agent.

The failure that watchdog cannot see
------------------------------------
A socket can die without either end noticing. A NAT or conntrack table drops an
idle flow with no RST, and what remains is a client that reports itself
connected, never resolves ``disconnected``, and never receives another update.
Writes may still appear to work — they force a fresh connection on demand — so
the process looks healthy from the inside and from its own logs while every
arriving message goes nowhere. On a VPS the flow is usually dropped after
fifteen to thirty minutes of quiet, which is precisely the reported symptom: a
deployed listener that answers for half an hour and then has to be restarted.

Waiting on the socket cannot detect that, so the health monitor watches the one
thing that actually matters — *when an update last arrived* — and, once the
connection has been quiet longer than ``idle_probe_after``, asks Telegram a
question. Silence answers nothing; a failed or timed-out probe does, and the
connection is torn down and rebuilt. If rebuilding fails
``max_recovery_attempts`` times, :attr:`TelegramClientManager.failed` is set so
the interface can exit and let a supervisor restart the process: a listener that
has stopped listening should not keep the port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tgagent.config.settings import TelegramSettings
from tgagent.errors import ConfigError, NotAuthorizedError, TelegramError
from tgagent.observability.logging import get_logger

log = get_logger(__name__)

_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 300.0


def parse_proxy(url: str) -> tuple[Any, ...] | None:
    """Convert a proxy URL into the tuple Telethon's ``proxy=`` expects.

    Accepts ``socks5://``, ``socks4://``, and ``http://`` with optional
    credentials. Returns ``None`` for an empty string so callers can pass a
    possibly-unset setting straight through.
    """
    if not url:
        return None

    # Validate the URL before importing PySocks, so a typo reports the typo
    # rather than a missing optional dependency.
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("socks5", "socks5h", "socks4", "http", "https"):
        raise ConfigError(
            f"Unsupported proxy scheme {parsed.scheme!r}. Use socks5://, socks4://, or http://."
        )
    if not parsed.hostname or not parsed.port:
        raise ConfigError("A proxy URL must include both host and port.")

    try:
        import socks  # PySocks
    except ImportError as exc:
        raise ConfigError(
            "A proxy is configured but PySocks is not installed. Install the extra "
            'with `pip install "tgagent[proxy]"`.'
        ) from exc

    kind = {
        "socks5": socks.SOCKS5,
        "socks5h": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }[scheme]

    if parsed.username:
        return (kind, parsed.hostname, parsed.port, True, parsed.username, parsed.password or "")
    return (kind, parsed.hostname, parsed.port)


class TelegramClientManager:
    """Constructs, connects, and supervises the MTProto client."""

    def __init__(self, settings: TelegramSettings, session_path: Path) -> None:
        self._settings = settings
        self._session_path = session_path
        self._client: Any = None
        self._watchdog: asyncio.Task[None] | None = None
        # An Event rather than a bool: this is read by the watchdog task and
        # written by stop(), i.e. genuinely cross-task state.
        self._closing = asyncio.Event()
        self._me: Any = None

        self._monitor: asyncio.Task[None] | None = None
        #: When something last arrived from Telegram, or a probe last proved the
        #: connection alive. Monotonic, because this is a duration question and a
        #: wall clock that steps backwards must not make a live client look stale.
        self._last_seen = 0.0
        self._recovery_failures = 0
        #: Set when recovery has given up. The interface waits on this and exits,
        #: rather than staying up in a state where it receives nothing.
        self.failed = asyncio.Event()

    # ------------------------------------------------------------ lifecycle --
    def build(self) -> Any:
        """Create the client without connecting. Separated so login can reuse it."""
        if self._client is not None:
            return self._client

        from telethon import TelegramClient

        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            # The session file is an authenticated credential; keep the
            # directory owner-only on platforms where that means something.
            with contextlib.suppress(OSError):
                self._session_path.parent.chmod(0o700)

        s = self._settings
        proxy = parse_proxy(s.proxy.get_secret_value()) if s.proxy else None

        # Telethon appends `.session`; pass the stem so the resulting filename
        # matches `Settings.session_path`.
        session_stem = str(self._session_path.with_suffix(""))

        self._client = TelegramClient(
            session_stem,
            s.api_id,
            s.api_hash.get_secret_value(),
            device_model=s.device_model,
            system_version=s.system_version,
            app_version=s.app_version,
            lang_code=s.lang_code,
            system_lang_code=s.lang_code,
            connection_retries=s.connection_retries,
            request_retries=s.request_retries,
            retry_delay=s.retry_delay,
            timeout=s.timeout,
            flood_sleep_threshold=s.flood_sleep_threshold,
            auto_reconnect=True,
            proxy=proxy,
        )
        return self._client

    async def start(self, *, require_authorization: bool = True) -> Any:
        """Connect and (optionally) insist that a valid session exists."""
        client = self.build()
        if not client.is_connected():
            try:
                await client.connect()
            except OSError as exc:
                raise TelegramError(
                    f"Could not reach Telegram: {exc}. Check connectivity or configure a proxy."
                ) from exc

        if require_authorization and not await client.is_user_authorized():
            raise NotAuthorizedError(
                "No authorised Telegram session was found. Run `tgagent login` first."
            )

        if require_authorization:
            self._me = await client.get_me()
            log.info(
                "telegram.connected",
                user_id=getattr(self._me, "id", None),
                username=getattr(self._me, "username", None),
            )
            self.note_activity()
            self._watch_updates()
            self._start_watchdog()
            self._start_monitor()

        return client

    async def stop(self) -> None:
        """Disconnect and stop supervising. Safe to call more than once."""
        self._closing.set()
        for name in ("_watchdog", "_monitor"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, name, None)

        if self._client is not None and self._client.is_connected():
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("telegram.disconnect_failed", error=str(exc))
        log.info("telegram.disconnected")

    # -------------------------------------------------------------- access ---
    @property
    def client(self) -> Any:
        if self._client is None:
            raise TelegramError("The Telegram client has not been started.")
        return self._client

    @property
    def me(self) -> Any:
        return self._me

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    async def is_authorized(self) -> bool:
        client = self.build()
        if not client.is_connected():
            await client.connect()
        return bool(await client.is_user_authorized())

    async def ensure_connected(self) -> None:
        """Reconnect on demand, used by the gateway before each call."""
        client = self.client
        if client.is_connected():
            return
        log.warning("telegram.reconnecting")
        await client.connect()
        if not await client.is_user_authorized():
            raise NotAuthorizedError("The Telegram session is no longer authorised.")

    # -------------------------------------------------------------- health ---
    @property
    def idle_seconds(self) -> float:
        """How long since anything was heard from Telegram.

        The number that distinguishes a quiet account from a dead connection —
        which nothing else in the process can tell apart.
        """
        if not self._last_seen:
            return 0.0
        return max(0.0, time.monotonic() - self._last_seen)

    @property
    def healthy(self) -> bool:
        """Connected, and heard from recently enough to believe it."""
        if not self.connected:
            return False
        return self.idle_seconds < self._settings.idle_probe_after * 2

    def note_activity(self) -> None:
        """Record that Telegram was heard from just now."""
        self._last_seen = time.monotonic()

    def _watch_updates(self) -> None:
        """Stamp every arriving update, whatever kind it is.

        A raw handler rather than the bridge's own: the bridge only sees new
        messages, and a connection carrying nothing but read receipts and typing
        notifications is a connection that is very much alive.
        """
        from telethon import events

        def _seen(_update: Any) -> None:
            self.note_activity()

        self._client.add_event_handler(_seen, events.Raw)

    def _start_monitor(self) -> None:
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._watch_health(), name="telegram-health")

    async def _watch_health(self) -> None:
        """Prove the connection is alive, or rebuild it."""
        settings = self._settings
        while not self._closing.is_set():
            await asyncio.sleep(settings.health_check_interval)
            if self._closing.is_set():
                return
            if not self.connected or self.idle_seconds < settings.idle_probe_after:
                continue

            log.info("telegram.probing", idle_seconds=round(self.idle_seconds))
            if await self._probe():
                # A successful probe *is* activity: it proves the connection was
                # alive at this moment, which is what the idle timer means.
                self.note_activity()
                continue

            await self._recover()

    async def _probe(self) -> bool:
        """Ask Telegram one cheap question, with a deadline.

        The deadline is the point. A dead socket does not refuse the request, it
        swallows it, so waiting forever is the same as not asking.
        """
        try:
            await asyncio.wait_for(self.client.get_me(), timeout=min(30.0, self._settings.timeout))
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure means "rebuild it"
            log.warning("telegram.probe_failed", error=str(exc), idle=round(self.idle_seconds))
            return False

    async def _recover(self) -> None:
        """Tear the connection down and build it again.

        A plain ``connect()`` is not enough: the client believes it is already
        connected, so it would return immediately and change nothing. Event
        handlers survive this — they belong to the client object, not the
        connection — so nothing needs re-registering.
        """
        log.error("telegram.connection_stale", idle_seconds=round(self.idle_seconds))
        try:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.error("telegram.session_revoked")
                self.failed.set()
                return

            self._me = await self.client.get_me()
            # Ask for what was missed while the socket was a black hole.
            if catch_up := getattr(self.client, "catch_up", None):
                with contextlib.suppress(Exception):
                    await catch_up()

            self.note_activity()
            self._recovery_failures = 0
            log.warning("telegram.connection_rebuilt")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next tick tries again
            self._recovery_failures += 1
            log.error(
                "telegram.recovery_failed",
                error=str(exc),
                attempt=self._recovery_failures,
                limit=self._settings.max_recovery_attempts,
            )
            if self._recovery_failures >= self._settings.max_recovery_attempts:
                log.error("telegram.giving_up")
                self.failed.set()

    # ------------------------------------------------------------ watchdog ---
    def _start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._closing.clear()
            self._watchdog = asyncio.create_task(self._supervise(), name="telegram-watchdog")

    async def _supervise(self) -> None:
        """Re-establish the connection whenever Telethon gives up on it."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing.is_set():
            try:
                await self.client.disconnected
            except asyncio.CancelledError:
                raise
            # The watchdog exists to survive arbitrary failures; if it
            # propagated one, reconnection would stop for good.
            except Exception as exc:  # noqa: BLE001
                log.debug("telegram.disconnect_wait_failed", error=str(exc))

            if self._closing.is_set():
                return

            log.warning("telegram.connection_lost", retry_in=delay)
            await asyncio.sleep(delay)
            try:
                await self.client.connect()
                if await self.client.is_user_authorized():
                    log.info("telegram.reconnected")
                    delay = _RECONNECT_BASE_DELAY
                    continue
                log.error("telegram.session_revoked")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram.reconnect_failed", error=str(exc), next_retry=delay)
                delay = min(_RECONNECT_MAX_DELAY, delay * 2)
