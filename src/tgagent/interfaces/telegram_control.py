"""Driving the agent from inside Telegram.

This is an interface in exactly the sense ``interfaces/__init__.py`` describes:
it drives :meth:`~tgagent.agent.runtime.AgentRuntime.run` and supplies a
:class:`~tgagent.security.confirm.ConfirmationProvider`. It knows nothing the CLI
does not, and the agent core knows nothing about it.

The idea is that you never leave Telegram. Sitting in any chat, you type::

    agent summarise what I missed here today

and the bridge hands that instruction to the agent along with *where it was
typed* — chat id, chat kind, the message id, and the message it replied to — so
"here", "this", and "them" all resolve without you spelling them out. The answer
comes back as a reply in the same chat.

Why this is not simply "read messages and obey them"
----------------------------------------------------
A chat is full of text written by other people. If arriving text could become an
instruction, anyone who can message the account could drive it. Three things keep
that from being true:

* **Authorship.** By default only the account owner's *own* outgoing messages are
  commands (``control.respond_to_self``). Letting anyone else in is an explicit
  list of senders, and the docstring on that setting says what it grants.
* **Framing.** The instruction enters the run as operator input. Everything that
  came from someone else — notably the replied-to message — is fenced as
  untrusted data by :mod:`tgagent.security.trust`, the same way tool output is.
* **A loop breaker.** Anything the agent sends is also an outgoing message, so a
  reply that happened to begin with the trigger word could feed itself. Commands
  are therefore rate-limited globally (``control.max_commands_per_minute``), one
  chat runs one command at a time, and the bridge ignores messages it sent
  itself.

Confirmations
-------------
A run started from a chat has a human attached, so CONFIRM decisions can be
answered rather than denied: the bridge asks in the originating chat and waits for
``yes`` or ``no``. Routing works through a :class:`~contextvars.ContextVar` set
around each run, which is what lets one shared provider serve concurrent runs in
different chats — the gateway calls ``confirm()`` deep inside the run, and the
context variable tells the provider which chat to ask.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tgagent.agent.events import AgentEvent, EventKind, RunResult
from tgagent.agent.runtime import AgentRuntime
from tgagent.config.settings import TelegramControlSettings
from tgagent.observability.logging import get_logger
from tgagent.risk import RiskTier
from tgagent.security.confirm import (
    CallbackConfirmation,
    ConfirmationOutcome,
    ConfirmationRequest,
)
from tgagent.security.trust import UntrustedContent, wrap_untrusted
from tgagent.storage.base import AuditRepository
from tgagent.storage.models import AuditEntry

log = get_logger(__name__)

#: Words that answer a confirmation prompt in the affirmative / negative.
_YES = frozenset({"y", "yes", "ok", "okay", "allow", "approve", "go", "do it", "👍"})
_NO = frozenset({"n", "no", "nope", "deny", "stop", "cancel", "don't", "dont", "👎"})

#: Instructions handled by the bridge itself rather than passed to the model.
_STOP_WORDS = frozenset({"stop", "cancel", "abort", "halt"})
_RESET_WORDS = frozenset({"reset", "new", "forget", "clear"})
_HELP_WORDS = frozenset({"help", "?", "usage"})

#: Characters that may stand in for the space after the trigger word.
_SEPARATORS = ":,-\u2013\u2014"

_HELP_TEXT = (
    "**tgagent**\n"
    "`{trigger} <instruction>` — run an instruction with this chat as context\n"
    "`{trigger} stop` — cancel the run in progress here\n"
    "`{trigger} reset` — start a fresh conversation for this chat\n"
    "`{trigger} help` — this message\n\n"
    "Reply to a message and the replied-to text is included as context."
)


# ----------------------------------------------------------------- parsing ----
def parse_command(text: str, trigger: str) -> str | None:
    """Extract the instruction from *text*, or ``None`` if it is not a command.

    The trigger must open the message and be followed by the instruction, so
    ordinary prose that merely mentions the word is not a command::

        parse_command("agent summarise this", "agent")   # → "summarise this"
        parse_command("Agent: do it", "agent")           # → "do it"
        parse_command("ask the agent about it", "agent") # → None
        parse_command("agent", "agent")                  # → None (no instruction)
    """
    stripped = text.strip()
    if len(stripped) <= len(trigger):
        return None
    if stripped[: len(trigger)].casefold() != trigger.casefold():
        return None

    rest = stripped[len(trigger) :]
    # A separator is required. Without one, "agentic pipelines are…" would parse
    # as the instruction "ic pipelines are…".
    if rest[0] in _SEPARATORS:
        rest = rest[1:]
    elif not rest[0].isspace():
        return None

    instruction = rest.strip()
    return instruction or None


@dataclass(slots=True, frozen=True)
class CommandSource:
    """Where a command came from — the context the agent gets for free."""

    chat_id: int
    message_id: int
    instruction: str
    chat_title: str = ""
    chat_kind: str = "chat"
    sender_id: int | None = None
    sender_name: str = ""
    sender_username: str | None = None
    from_self: bool = False
    date: datetime | None = None
    reply_to_message_id: int | None = None
    reply_to_text: str = ""
    reply_to_sender: str = ""

    @property
    def key(self) -> str:
        """Stable identifier for the chat, used for conversations and locks."""
        return str(self.chat_id)


def build_prompt(source: CommandSource, settings: TelegramControlSettings) -> str:
    """Render the operator instruction plus its Telegram context.

    The context header and the instruction are trusted — the account owner typed
    them. The replied-to message was written by somebody else, so it is fenced as
    untrusted data even when that somebody is the owner: the fence is what tells
    the model the difference between "what I was asked to do" and "text I am
    being asked to look at".
    """
    who = source.sender_name or "unknown"
    if source.sender_username:
        who += f" (@{source.sender_username})"
    if source.from_self:
        who += " — you, the account owner"

    lines = [
        "This request came from a Telegram chat. Its context:",
        f"- chat: {source.chat_title or 'untitled'} · {source.chat_kind} · id {source.chat_id}",
        f"- from: {who}" + (f", id {source.sender_id}" if source.sender_id else ""),
        f"- command message id: {source.message_id}",
    ]
    if source.date is not None:
        lines.append(f"- sent at: {source.date.isoformat()}")
    lines.append(
        f"\nUnless the instruction names another chat, chat {source.chat_id} is what "
        f'"here", "this chat", and "them" refer to. Your answer is delivered to '
        f"that chat automatically — do not send it as a message yourself.\n"
    )
    lines.append(f"Instruction: {source.instruction}")

    if source.reply_to_text and settings.include_reply_context:
        excerpt = source.reply_to_text[: settings.reply_context_chars]
        if len(source.reply_to_text) > settings.reply_context_chars:
            excerpt += " …[truncated]"
        fenced = wrap_untrusted(
            UntrustedContent(
                text=excerpt,
                source=f"telegram:chat/{source.chat_id}/message/{source.reply_to_message_id}",
            )
        )
        author = f" from {source.reply_to_sender}" if source.reply_to_sender else ""
        lines.append(
            f"\nThe command replied to message {source.reply_to_message_id}{author}. "
            f"Its text, as data:\n{fenced}"
        )

    return "\n".join(lines)


# ----------------------------------------------------------- confirmations ----
@dataclass(slots=True)
class _PendingConfirmation:
    future: asyncio.Future[bool]
    request: ConfirmationRequest


#: The chat the current run belongs to. Set around each run so a single shared
#: confirmation provider can route prompts correctly across concurrent runs.
_active_source: ContextVar[CommandSource | None] = ContextVar(
    "tgagent_control_source", default=None
)


class ChatConfirmation:
    """Asks for confirmation in the chat the command came from.

    Delegates timeout handling and "yes to everything like this for this run" to
    :class:`~tgagent.security.confirm.CallbackConfirmation` rather than
    reimplementing either.
    """

    def __init__(self, bridge: TelegramControlBridge, *, timeout: float) -> None:
        self._bridge = bridge
        self._inner = CallbackConfirmation(self._ask, timeout=timeout)

    @property
    def interactive(self) -> bool:
        # True only while a chat-initiated run is on the stack. A scheduled run
        # shares this provider and genuinely has nobody to ask, so it must report
        # False and let the policy's non-interactive decision apply.
        return _active_source.get() is not None

    def reset(self) -> None:
        self._inner.reset()

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        if _active_source.get() is None:
            return ConfirmationOutcome(
                approved=False,
                reason="No chat is attached to this run, so nobody could be asked.",
            )
        return await self._inner.confirm(request)

    async def _ask(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        source = _active_source.get()
        if source is None:  # pragma: no cover - guarded by confirm() above
            return ConfirmationOutcome(approved=False, reason="No chat attached.")
        return await self._bridge.ask_in_chat(source, request)


# ------------------------------------------------------------------ bridge ----
@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[None]
    cancel: asyncio.Event


class TelegramControlBridge:
    """Listens for trigger messages and runs the agent on them."""

    def __init__(
        self,
        manager: Any,
        runtime_factory: Any,
        settings: TelegramControlSettings,
        *,
        me_id: int | None = None,
        audit: AuditRepository | None = None,
        confirmation_timeout: float = 300.0,
        log_arguments: bool = False,
    ) -> None:
        self._manager = manager
        self._runtime_factory = runtime_factory
        self._settings = settings
        self._me_id = me_id
        self._audit = audit
        self._log_arguments = log_arguments

        self.confirmations = ChatConfirmation(self, timeout=confirmation_timeout)

        self._semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        self._active: dict[str, _ActiveRun] = {}
        self._pending_confirmations: dict[str, _PendingConfirmation] = {}
        self._conversations: dict[str, str] = {}
        #: Accept timestamps, for the global loop breaker.
        self._accepted: deque[float] = deque()
        #: Messages this bridge sent, so its own output can never be a command.
        self._own_messages: deque[tuple[int, int]] = deque(maxlen=256)
        self._handler: Any = None
        self._stopped = asyncio.Event()

    # ---------------------------------------------------------- lifecycle ----
    async def start(self) -> None:
        """Register the Telethon event handler."""
        from telethon import events

        client = self._manager.client
        if self._me_id is None:
            self._me_id = getattr(self._manager.me, "id", None)

        self._handler = self._on_new_message
        client.add_event_handler(self._handler, events.NewMessage())
        log.info(
            "control.listening",
            trigger=self._settings.trigger,
            respond_to_self=self._settings.respond_to_self,
            allowed_senders=len(self._settings.allowed_senders),
        )

    async def stop(self) -> None:
        """Deregister, cancel in-flight runs, and refuse pending confirmations."""
        self._stopped.set()
        if self._handler is not None:
            with contextlib.suppress(Exception):
                self._manager.client.remove_event_handler(self._handler)
            self._handler = None

        for pending in list(self._pending_confirmations.values()):
            if not pending.future.done():
                pending.future.set_result(False)
        self._pending_confirmations.clear()

        for run in list(self._active.values()):
            run.cancel.set()
            run.task.cancel()
        # Runs clean up after themselves in _run_command's finally block; wait so
        # the client is not torn out from under a reply that is being sent.
        if self._active:
            await asyncio.gather(*(r.task for r in self._active.values()), return_exceptions=True)
        self._active.clear()
        log.info("control.stopped")

    async def wait_closed(self) -> None:
        await self._stopped.wait()

    # ------------------------------------------------------------ dispatch ----
    async def _on_new_message(self, event: Any) -> None:
        """Telethon entry point. Never raises — a handler that does is silent."""
        try:
            await self.handle_event(event)
        except asyncio.CancelledError:
            raise
        # One malformed message must never stop the bridge listening.
        except Exception as exc:
            log.error("control.handler_failed", error=str(exc), exc_info=True)

    async def handle_event(self, event: Any) -> bool:
        """Classify one message. Returns True if it started or answered something.

        Public so tests (and any other transport) can drive the bridge without
        Telethon in the picture.
        """
        text = _text_of(event)
        if not text:
            return False

        chat_id = _chat_id_of(event)
        message_id = _int_or_none(getattr(event, "id", None)) or _int_or_none(
            getattr(getattr(event, "message", None), "id", None)
        )
        if chat_id is None or message_id is None:
            return False
        if (chat_id, message_id) in self._own_messages:
            return False

        # A pending confirmation takes precedence: "no" is an answer, not an
        # instruction, and must not need the trigger word in front of it.
        if await self._maybe_answer_confirmation(chat_id, text, event):
            return True

        instruction = parse_command(text, self._settings.trigger)
        if instruction is None:
            return False

        source = await self._describe(event, chat_id, message_id, instruction)
        refusal = await self._authorise(source, event)
        if refusal is not None:
            log.info("control.command_refused", chat=chat_id, reason=refusal)
            await self._record(source, decision="deny", error=refusal)
            return False

        await self._record(source, decision="allow", error=None)
        return await self._dispatch(source)

    async def _dispatch(self, source: CommandSource) -> bool:
        """Handle a built-in word, or start a run."""
        word = source.instruction.strip().casefold().rstrip(".!")

        if word in _STOP_WORDS:
            run = self._active.get(source.key)
            if run is None:
                await self._reply(source, "Nothing is running in this chat.")
            else:
                run.cancel.set()
                await self._reply(source, "Stopping.")
            return True

        if word in _RESET_WORDS:
            self._reset_conversation(source)
            await self._reply(source, "Started a fresh conversation for this chat.")
            return True

        if word in _HELP_WORDS:
            await self._reply(source, _HELP_TEXT.format(trigger=self._settings.trigger))
            return True

        if source.key in self._active:
            await self._reply(
                source,
                f"Still working on the previous request here. "
                f"Send `{self._settings.trigger} stop` to cancel it.",
            )
            return False

        cancel = asyncio.Event()
        task = asyncio.create_task(
            self._run_command(source, cancel), name=f"control-run-{source.chat_id}"
        )
        self._active[source.key] = _ActiveRun(task=task, cancel=cancel)
        return True

    # ----------------------------------------------------------- the run ------
    async def _run_command(self, source: CommandSource, cancel: asyncio.Event) -> None:
        token = _active_source.set(source)
        try:
            async with self._semaphore:
                if cancel.is_set():
                    return
                await self._execute(source, cancel)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._reply(source, "Cancelled.")
            raise
        # The operator gets told what broke; the bridge keeps listening.
        except Exception as exc:
            log.error("control.run_failed", chat=source.chat_id, error=str(exc), exc_info=True)
            with contextlib.suppress(Exception):
                await self._reply(source, f"⚠️ That failed: {exc}")
        finally:
            _active_source.reset(token)
            self.confirmations.reset()
            self._active.pop(source.key, None)

    async def _execute(self, source: CommandSource, cancel: asyncio.Event) -> None:
        runtime: AgentRuntime = self._runtime_factory()
        conversation_id = self._conversation_id(source)

        log.info(
            "control.run_started",
            chat=source.chat_id,
            conversation=conversation_id,
            chars=len(source.instruction),
        )
        async with self._typing(source):
            result: RunResult = await runtime.run(
                build_prompt(source, self._settings),
                conversation_id=conversation_id,
                interactive=True,
                cancel=cancel,
                on_event=self._on_agent_event,
            )

        self._conversations[self._conversation_key(source)] = result.conversation_id
        await self._reply(source, _format_answer(result))
        log.info(
            "control.run_finished",
            chat=source.chat_id,
            steps=result.steps,
            tool_calls=result.tool_calls,
            stopped_because=result.stopped_because,
        )

    def _on_agent_event(self, event: AgentEvent) -> None:
        # The bridge deliberately does not narrate tool calls into the chat:
        # every one would be an outgoing message, which is both noisy and the
        # thing the loop breaker exists to bound. Progress goes to the log.
        if event.kind is EventKind.ERROR:
            log.warning("control.run_error", detail=event.text)

    def _conversation_key(self, source: CommandSource) -> str:
        """Which conversation slot this chat's runs belong to."""
        return "global" if self._settings.conversation_scope == "global" else source.key

    def _conversation_id(self, source: CommandSource) -> str:
        """The conversation a run in this chat continues.

        The default is derived from the chat id rather than random, so restarting
        the bridge picks a chat's thread back up where it left off.
        """
        key = self._conversation_key(source)
        default = "tg-control-global" if key == "global" else f"tg-chat-{source.chat_id}"
        return self._conversations.setdefault(key, default)

    def _reset_conversation(self, source: CommandSource) -> None:
        """Point this chat at a fresh conversation.

        A new id rather than a deleted entry: the default id is *derived* from the
        chat, so deleting the entry would recompute the same id and quietly
        continue the conversation the operator just asked to leave behind.
        """
        key = self._conversation_key(source)
        stem = "tg-control-global" if key == "global" else f"tg-chat-{source.chat_id}"
        self._conversations[key] = f"{stem}-{uuid.uuid4().hex[:8]}"

    # ---------------------------------------------------- authorisation ------
    async def _authorise(self, source: CommandSource, event: Any) -> str | None:
        """Return ``None`` to accept, or the reason for refusing."""
        s = self._settings

        if not await self._sender_allowed(source, event):
            return "sender is not authorised to issue commands"

        chat_names = _chat_names(source, event)
        if s.ignored_chats and _matches(s.ignored_chats, chat_names):
            return "chat is on control.ignored_chats"
        if s.allowed_chats and not _matches(s.allowed_chats, chat_names):
            return "chat is not on control.allowed_chats"

        if not self._take_rate_token():
            return f"rate limit: more than {s.max_commands_per_minute} commands in a minute"
        return None

    async def _sender_allowed(self, source: CommandSource, event: Any) -> bool:
        if source.from_self:
            return self._settings.respond_to_self
        allowed = self._settings.allowed_senders
        if not allowed:
            return False
        names: list[str] = []
        if source.sender_id is not None:
            names.append(str(source.sender_id))
        if source.sender_username:
            names.append(source.sender_username)
        # Only pay for a round trip when the list actually holds a name to match.
        elif any(not _looks_numeric(entry) for entry in allowed) and (
            username := await _sender_username(event)
        ):
            names.append(username)
        return _matches(allowed, names)

    def _take_rate_token(self) -> bool:
        """Consume one slot from the per-minute budget, or refuse."""
        now = time.monotonic()
        while self._accepted and now - self._accepted[0] > 60.0:
            self._accepted.popleft()
        if len(self._accepted) >= self._settings.max_commands_per_minute:
            log.error(
                "control.rate_limited",
                limit=self._settings.max_commands_per_minute,
                window_seconds=60,
            )
            return False
        self._accepted.append(now)
        return True

    # ---------------------------------------------------- confirmations ------
    async def ask_in_chat(
        self, source: CommandSource, request: ConfirmationRequest
    ) -> ConfirmationOutcome:
        """Post a confirmation prompt and wait for a reply in that chat."""
        if not self._settings.confirm_in_chat:
            return ConfirmationOutcome(
                approved=False, reason="control.confirm_in_chat is disabled."
            )

        existing = self._pending_confirmations.get(source.key)
        if existing is not None and not existing.future.done():
            return ConfirmationOutcome(
                approved=False, reason="Another confirmation is already open in this chat."
            )

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_confirmations[source.key] = _PendingConfirmation(future, request)
        try:
            await self._reply(
                source,
                f"⚠️ **Confirmation needed** ({request.risk.value})\n"
                f"```\n{request.render()}\n```\n"
                f"Reply `yes` to allow or `no` to refuse.",
            )
            approved = await future
        finally:
            self._pending_confirmations.pop(source.key, None)

        return ConfirmationOutcome(
            approved=approved,
            reason="Approved in the chat." if approved else "Refused in the chat.",
        )

    async def _maybe_answer_confirmation(self, chat_id: int, text: str, event: Any) -> bool:
        pending = self._pending_confirmations.get(str(chat_id))
        if pending is None or pending.future.done():
            return False

        word = text.strip().casefold().rstrip(".!")
        if word in _YES:
            approved = True
        elif word in _NO:
            approved = False
        else:
            return False

        # An answer carries the same authority as a command, so it needs the same
        # check — otherwise a bystander could approve a destructive operation.
        source = await self._describe(event, chat_id, 0, word)
        if not await self._sender_allowed(source, event):
            log.warning("control.confirmation_answer_ignored", chat=chat_id)
            return False

        pending.future.set_result(approved)
        log.info("control.confirmation_answered", chat=chat_id, approved=approved)
        return True

    # --------------------------------------------------------------- io ------
    async def _reply(self, source: CommandSource, text: str) -> None:
        """Deliver text to the chat, split across Telegram's length limit.

        Sent through the client rather than the gateway on purpose: this is the
        control plane answering its operator, not the agent acting on the
        account. Routing it through the permission engine would make every reply
        an ``EXTERNALLY_VISIBLE`` write needing confirmation — and the
        confirmation would be delivered by the very call being confirmed.
        """
        client = self._manager.client
        reply_to = source.message_id if self._settings.reply_to_command else None
        for index, chunk in enumerate(_chunk(text, self._settings.max_reply_chars)):
            sent = await client.send_message(
                source.chat_id,
                chunk,
                reply_to=reply_to if index == 0 else None,
                link_preview=False,
            )
            for message_id in _sent_ids(sent):
                self._own_messages.append((source.chat_id, message_id))

    @contextlib.asynccontextmanager
    async def _typing(self, source: CommandSource) -> AsyncIterator[None]:
        """Show "typing…" for the duration of a run, if the client supports it.

        The indicator is cosmetic, so a client that cannot show one, or fails
        trying, must not affect the run. What it equally must not do is interfere
        with an exception coming *out* of the body — hence the single ``yield`` on
        every path. Yielding a second time in an error handler turns whatever the
        run raised into an opaque "generator didn't stop after athrow()".
        """
        action = getattr(self._manager.client, "action", None)
        entered: Any = None
        if self._settings.typing_indicator and action is not None:
            try:
                entered = action(source.chat_id, "typing")
                await entered.__aenter__()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any client that cannot is fine
                log.debug("control.typing_failed", error=str(exc))
                entered = None
        try:
            yield
        finally:
            if entered is not None:
                with contextlib.suppress(Exception):
                    await entered.__aexit__(None, None, None)

    async def _record(self, source: CommandSource, *, decision: str, error: str | None) -> None:
        if self._audit is None:
            return
        entry = AuditEntry(
            run_id="control",
            conversation_id=self._conversations.get(source.key),
            method="control.command",
            risk=RiskTier.READ_ONLY.value,
            decision=decision,
            target=f"chat/{source.chat_id}",
            argument_digest=hashlib.sha256(source.instruction.encode()).hexdigest()[:16],
            argument_preview=source.instruction[:500] if self._log_arguments else None,
            succeeded=decision == "allow",
            error=error,
            origin="control",
        )
        try:
            await self._audit.record(entry)
        except Exception as exc:  # noqa: BLE001 - auditing must not break listening
            log.error("control.audit_failed", error=str(exc))

    # ------------------------------------------------------- description -----
    async def _describe(
        self, event: Any, chat_id: int, message_id: int, instruction: str
    ) -> CommandSource:
        message = getattr(event, "message", None) or event
        sender_id = _int_or_none(getattr(event, "sender_id", None)) or _int_or_none(
            getattr(message, "sender_id", None)
        )
        outgoing = bool(getattr(message, "out", False))
        from_self = outgoing or (
            self._me_id is not None and sender_id is not None and sender_id == self._me_id
        )

        chat = getattr(event, "chat", None)
        sender = getattr(event, "sender", None)

        reply_id: int | None = None
        reply_text = ""
        reply_sender = ""
        if self._settings.include_reply_context:
            reply_id, reply_text, reply_sender = await _reply_context(event, message)

        return CommandSource(
            chat_id=chat_id,
            message_id=message_id,
            instruction=instruction,
            chat_title=_chat_title(event, chat),
            chat_kind=_chat_kind(event),
            sender_id=sender_id,
            sender_name=_display_name(sender) or ("you" if from_self else ""),
            sender_username=getattr(sender, "username", None),
            from_self=from_self,
            date=getattr(message, "date", None),
            reply_to_message_id=reply_id,
            reply_to_text=reply_text,
            reply_to_sender=reply_sender,
        )


# ---------------------------------------------------------------- helpers -----
def _format_answer(result: RunResult) -> str:
    answer = result.answer.strip() or "(no answer)"
    if result.cancelled:
        return "Cancelled."
    if result.stopped_because:
        answer += f"\n\n_stopped: {result.stopped_because}_"
    if result.errors:
        answer += "\n" + "\n".join(f"_! {message}_" for message in result.errors[:3])
    return answer


def _chunk(text: str, limit: int) -> list[str]:
    """Split *text* for Telegram, preferring line then word boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _text_of(event: Any) -> str:
    for attribute in ("raw_text", "text"):
        value = getattr(event, attribute, None)
        if isinstance(value, str) and value:
            return value
    message = getattr(event, "message", None)
    value = getattr(message, "message", None)
    return value if isinstance(value, str) else ""


def _chat_id_of(event: Any) -> int | None:
    return _int_or_none(getattr(event, "chat_id", None)) or _int_or_none(
        getattr(getattr(event, "message", None), "chat_id", None)
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chat_title(event: Any, chat: Any) -> str:
    for candidate in (chat, getattr(event, "chat", None)):
        if candidate is None:
            continue
        title = getattr(candidate, "title", None) or _display_name(candidate)
        if title:
            return str(title)
    return ""


def _chat_kind(event: Any) -> str:
    if getattr(event, "is_private", False):
        return "private chat"
    if getattr(event, "is_channel", False) and not getattr(event, "is_group", False):
        return "channel"
    if getattr(event, "is_group", False):
        return "group"
    return "chat"


def _display_name(entity: Any) -> str:
    if entity is None:
        return ""
    if title := getattr(entity, "title", None):
        return str(title)
    parts = [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""]
    return " ".join(part for part in parts if part).strip()


async def _reply_context(event: Any, message: Any) -> tuple[int | None, str, str]:
    """Fetch the replied-to message, tolerating a client that cannot."""
    reply_to = getattr(message, "reply_to", None)
    reply_id = _int_or_none(getattr(reply_to, "reply_to_msg_id", None))
    getter = getattr(event, "get_reply_message", None)
    if reply_id is None or getter is None:
        return reply_id, "", ""
    try:
        replied = await getter()
    except Exception as exc:  # noqa: BLE001 - context is a nicety, not a requirement
        log.debug("control.reply_fetch_failed", error=str(exc))
        return reply_id, "", ""
    if replied is None:
        return reply_id, "", ""
    text = getattr(replied, "message", None) or getattr(replied, "raw_text", None) or ""
    return reply_id, str(text), _display_name(getattr(replied, "sender", None))


async def _sender_username(event: Any) -> str | None:
    getter = getattr(event, "get_sender", None)
    if getter is None:
        return None
    try:
        sender = await getter()
    except Exception as exc:  # noqa: BLE001 - an unresolvable sender is just not allowed
        log.debug("control.sender_fetch_failed", error=str(exc))
        return None
    username = getattr(sender, "username", None)
    return str(username) if username else None


def _chat_names(source: CommandSource, event: Any) -> list[str]:
    names = [str(source.chat_id)]
    chat = getattr(event, "chat", None)
    if username := getattr(chat, "username", None):
        names.append(str(username))
    if source.chat_title:
        names.append(source.chat_title)
    return names


def _matches(candidates: Iterable[str], values: Sequence[str]) -> bool:
    wanted = {_normalise(value) for value in values if value}
    return any(_normalise(candidate) in wanted for candidate in candidates if candidate)


def _normalise(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def _looks_numeric(value: str) -> bool:
    return value.strip().lstrip("-").isdigit()


def _sent_ids(sent: Any) -> list[int]:
    """Message ids from whatever ``send_message`` returned (one, or a list)."""
    items = sent if isinstance(sent, (list, tuple)) else [sent]
    return [message_id for item in items if (message_id := _int_or_none(getattr(item, "id", None)))]


__all__ = [
    "ChatConfirmation",
    "CommandSource",
    "TelegramControlBridge",
    "build_prompt",
    "parse_command",
]
