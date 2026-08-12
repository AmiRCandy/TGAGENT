"""Interactive user-account sign-in.

Telegram's user-account login is a multi-step conversation: request a code,
submit the code, and — if the account has two-factor authentication — submit the
cloud password. Each step can fail in ways the caller has to react to.

The credential prompts are *callbacks*, so the CLI can read from a terminal, a
web UI can render a form, and a test can supply canned values. Nothing here
reads stdin or prints, which is what keeps the flow reusable across interfaces.
Each wait for a credential is bounded by ``login_timeout``: a prompt nobody
answers must fail the login, not hang the process holding it open.

Secrets handled here — code, password, and the resulting session — are never
logged, and the session file is written owner-only where the platform supports
it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tgagent.errors import AuthenticationError, ConfigError
from tgagent.observability.logging import get_logger
from tgagent.telegram.client import TelegramClientManager

log = get_logger(__name__)

#: ``async () -> str``. Must not echo what it collects.
CredentialPrompt = Callable[[], Awaitable[str]]


@dataclass(slots=True)
class LoginResult:
    user_id: int
    username: str | None
    first_name: str | None
    phone_last4: str | None
    session_path: Path
    was_already_authorized: bool = False


class LoginFlow:
    """Drives the sign-in conversation for a real user account."""

    def __init__(
        self,
        manager: TelegramClientManager,
        *,
        phone: str | None,
        request_code: CredentialPrompt | None = None,
        request_password: CredentialPrompt | None = None,
        request_phone: CredentialPrompt | None = None,
        timeout: float | None = None,
    ) -> None:
        self._manager = manager
        self._phone = phone
        self._request_code = request_code
        self._request_password = request_password
        self._request_phone = request_phone
        # Defaulting from the client's own settings means every caller inherits
        # the configured bound without having to thread it through.
        self._timeout = timeout if timeout is not None else _configured_timeout(manager)

    async def run(self) -> LoginResult:
        """Sign in, or report that a valid session already exists."""
        from telethon import errors

        client = self._manager.build()
        if not client.is_connected():
            await client.connect()

        if await client.is_user_authorized():
            me = await client.get_me()
            log.info("auth.already_authorized", user_id=getattr(me, "id", None))
            return self._result(me, was_already_authorized=True)

        phone = await self._resolve_phone()

        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError as exc:
            raise AuthenticationError(
                f"Telegram rejected the phone number {phone!r}. Use E.164 format, e.g. +15551234567."
            ) from exc
        except errors.PhoneNumberBannedError as exc:
            raise AuthenticationError("That phone number is banned from Telegram.") from exc
        except errors.FloodWaitError as exc:
            raise AuthenticationError(
                f"Too many login attempts. Telegram asks you to wait {exc.seconds}s."
            ) from exc

        log.info("auth.code_sent", phone_last4=phone[-4:], code_type=type(sent.type).__name__)

        code = await self._prompt(self._request_code, "login code")
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            await self._sign_in_with_password()
        except errors.PhoneCodeInvalidError as exc:
            raise AuthenticationError("That login code is incorrect.") from exc
        except errors.PhoneCodeExpiredError as exc:
            raise AuthenticationError("That login code has expired. Start again.") from exc
        except errors.PhoneNumberUnoccupiedError as exc:
            raise AuthenticationError(
                "No Telegram account exists for that number. Create one in an official "
                "client first — tgagent deliberately does not sign up new accounts."
            ) from exc

        if not await client.is_user_authorized():
            raise AuthenticationError("Sign-in completed but the session is not authorised.")

        self._harden_session_file()
        me = await client.get_me()
        log.info("auth.signed_in", user_id=getattr(me, "id", None))
        return self._result(me)

    async def logout(self) -> bool:
        """Revoke the session server-side and delete the local file.

        Revoking on the server matters: deleting only the local file leaves an
        authorised session listed on the account.
        """
        client = self._manager.build()
        if not client.is_connected():
            await client.connect()

        revoked = False
        if await client.is_user_authorized():
            revoked = bool(await client.log_out())

        await self._manager.stop()

        session_file = self._manager_session_path()
        for path in (session_file, session_file.with_suffix(".session-journal")):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

        log.info("auth.logged_out", revoked=revoked)
        return revoked

    # ------------------------------------------------------------ internals --
    async def _sign_in_with_password(self) -> None:
        from telethon import errors

        client = self._manager.client
        password = await self._prompt(self._request_password, "two-factor password", hint_2fa=True)
        try:
            await client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise AuthenticationError("That two-factor password is incorrect.") from exc

    async def _resolve_phone(self) -> str:
        if self._phone:
            return self._phone
        if self._request_phone is None:
            raise ConfigError(
                "No phone number configured. Set TGAGENT_TELEGRAM__PHONE or supply a phone prompt."
            )
        phone = (await self._collect(self._request_phone, "phone number")).strip()
        if not phone:
            raise AuthenticationError("A phone number is required to sign in.")
        return phone

    async def _prompt(
        self, prompt: CredentialPrompt | None, what: str, *, hint_2fa: bool = False
    ) -> str:
        if prompt is None:
            extra = " This account has two-factor authentication enabled." if hint_2fa else ""
            raise AuthenticationError(f"A {what} is required but no prompt was supplied.{extra}")
        value = (await self._collect(prompt, what)).strip()
        if not value:
            raise AuthenticationError(f"An empty {what} was supplied.")
        return value

    async def _collect(self, prompt: CredentialPrompt, what: str) -> str:
        """Await one credential, bounded by ``login_timeout``.

        Telegram's login code expires on its own, so waiting for it forever only
        ever ends in a hung process — one holding a half-finished sign-in.
        """
        if self._timeout is None:
            return await prompt()
        try:
            return await asyncio.wait_for(prompt(), self._timeout)
        except TimeoutError as exc:
            raise AuthenticationError(
                f"No {what} was supplied within {self._timeout:g}s, so the sign-in was "
                f"abandoned. Run the login again when you are ready to enter it, or "
                f"raise TGAGENT_TELEGRAM__LOGIN_TIMEOUT."
            ) from exc

    def _manager_session_path(self) -> Path:
        return Path(self._manager._session_path)  # noqa: SLF001 - same subsystem

    def _harden_session_file(self) -> None:
        """Make the session file owner-only where the OS supports it."""
        if os.name == "nt":
            return
        with contextlib.suppress(OSError):
            self._manager_session_path().chmod(0o600)

    def _result(self, me: Any, *, was_already_authorized: bool = False) -> LoginResult:
        phone = getattr(me, "phone", None)
        return LoginResult(
            user_id=int(getattr(me, "id", 0)),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            phone_last4=str(phone)[-4:] if phone else None,
            session_path=self._manager_session_path(),
            was_already_authorized=was_already_authorized,
        )


def _configured_timeout(manager: TelegramClientManager) -> float | None:
    """``login_timeout`` from the client's settings, or ``None`` if unavailable."""
    settings = getattr(manager, "_settings", None)  # same subsystem, private by convention
    value = getattr(settings, "login_timeout", None)
    return float(value) if value else None
