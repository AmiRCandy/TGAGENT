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
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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
        self._closing = False
        self._me: Any = None

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
            self._start_watchdog()

        return client

    async def stop(self) -> None:
        """Disconnect and stop supervising. Safe to call more than once."""
        self._closing = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None

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

    # ------------------------------------------------------------ watchdog ---
    def _start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._closing = False
            self._watchdog = asyncio.create_task(self._supervise(), name="telegram-watchdog")

    async def _supervise(self) -> None:
        """Re-establish the connection whenever Telethon gives up on it."""
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self.client.disconnected
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the point is to survive anything
                pass

            if self._closing:
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
