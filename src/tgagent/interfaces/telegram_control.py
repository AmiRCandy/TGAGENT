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

Knowing it is alive
-------------------
A run can take a minute, and over a chat window silence is ambiguous: a model
still thinking looks exactly like a bridge that died. Two things answer that
without narrating every tool call into the chat:

* **A status message.** The command is acknowledged immediately with one message,
  which is then *edited* every ``control.progress_interval`` seconds with what the
  run is doing, and finally edited into the answer itself. Editing rather than
  sending is what keeps this from being the flood the loop breaker exists to
  prevent: one message per run, however long it runs.
* **``agent ping``.** Answered by the bridge itself — no model, no tokens — with
  the Telegram round trip, how late the command arrived, and how long the process
  has been listening. It is the one command that still works when the LLM is
  misconfigured, which is exactly when you want to ask.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from tgagent.agent.events import AgentEvent, EventKind, RunResult
from tgagent.agent.runtime import AgentRuntime
from tgagent.config.settings import TelegramControlSettings
from tgagent.errors import TgAgentError
from tgagent.interfaces.admin import RuntimeAdmin
from tgagent.interfaces.autoreply import (
    STOPPED_BY_OPERATOR,
    AutoReplyWatcher,
    IncomingMessage,
    describe_watch,
)
from tgagent.observability.logging import get_logger
from tgagent.risk import RiskTier
from tgagent.security.confirm import (
    CallbackConfirmation,
    ConfirmationOutcome,
    ConfirmationRequest,
)
from tgagent.security.trust import UntrustedContent, wrap_untrusted
from tgagent.storage.base import AuditRepository
from tgagent.storage.models import AuditEntry, ChatWatch

log = get_logger(__name__)

#: Words that answer a confirmation prompt in the affirmative / negative.
_YES = frozenset({"y", "yes", "ok", "okay", "allow", "approve", "go", "do it", "👍"})
_NO = frozenset({"n", "no", "nope", "deny", "stop", "cancel", "don't", "dont", "👎"})

#: Instructions handled by the bridge itself rather than passed to the model.
_STOP_WORDS = frozenset({"stop", "cancel", "abort", "halt"})
_RESET_WORDS = frozenset({"reset", "new", "forget", "clear"})
_HELP_WORDS = frozenset({"help", "?", "usage"})
#: A liveness check, answered by the bridge without going near the model.
_PING_WORDS = frozenset({"ping", "alive", "status"})
#: Automatic replies: what is running, and the kill switch. Both are answered
#: here rather than by the model, because the day you most want to stop the
#: account answering for you is the day the model is what went wrong.
_WATCH_WORDS = frozenset({"watches", "watching", "autoreply"})
_UNWATCH_WORDS = frozenset(
    {"unwatch", "autoreply off", "stop replying", "stop autoreply", "stop answering"}
)
#: Administration, matched as the *first word* because these take arguments.
#: Owner-only, and answered here rather than by a tool — see
#: :mod:`tgagent.interfaces.admin` for why that distinction is the security model.
_ADMIN_WORDS = frozenset({"policy", "permissions", "llm", "model"})
#: "I am about to be unreachable." Answered without the model, because you are
#: boarding.
_FLIGHT_WORDS = frozenset({"flight", "away", "afk"})

#: Frames for the status message, so consecutive edits differ visibly.
_PULSE = ("⏳", "⌛")

#: Characters that may stand in for the space after the trigger word.
_SEPARATORS = ":,-\u2013\u2014"

_HELP_TEXT = (
    "**tgagent**\n"
    "`{trigger} <instruction>` — run an instruction with this chat as context\n"
    "`{trigger} ping` — check the bridge is alive, and how fast\n"
    "`{trigger} stop` — cancel the run in progress here\n"
    "`{trigger} reset` — start a fresh conversation for this chat\n"
    "`{trigger} help` — this message\n"
)

#: Appended to the help only where automatic replies are switched on.
_HELP_AUTOREPLY = (
    "`{trigger} flight on 3` — answer my private chats for three hours\n"
    "`{trigger} flight off` — landed; stop\n"
    "`{trigger} watches` — which chats are being answered for you\n"
    "`{trigger} unwatch` — stop answering all of them, now\n"
)

#: Appended for the account owner only, because only they can use it.
_HELP_ADMIN = (
    "`{trigger} policy` — what I am allowed to do, and change it\n"
    "`{trigger} llm` — which model I am using, and change it\n"
)

_HELP_FOOTER = "\nReply to a message and the replied-to text is included as context."


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

    #: The chat as a *resolvable* peer, taken from the triggering event.
    #:
    #: Load-bearing, not a convenience. Telethon cannot turn a bare user id into
    #: an ``InputPeerUser`` on its own — that needs an ``access_hash``, which only
    #: exists in its entity cache — so replying to ``chat_id`` fails with
    #: "Could not find the input entity for PeerUser(...)" for any chat the
    #: session has not already fetched. Worse, it fails *intermittently*: one
    #: ``get_dialogs`` anywhere in the process warms the cache and hides it. The
    #: event that carried the command always knows its own peer, so it is kept.
    peer: Any = None

    @property
    def key(self) -> str:
        """Stable identifier for the chat, used for conversations and locks."""
        return str(self.chat_id)

    @property
    def destination(self) -> Any:
        """What to hand Telethon when addressing this chat."""
        return self.peer if self.peer is not None else self.chat_id


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


# --------------------------------------------------------------- progress ----
@dataclass(slots=True)
class _Progress:
    """What a run is doing, at the resolution a chat window deserves.

    Mutated by :meth:`_StatusMessage.observe` as runtime events arrive and read by
    the ticker that does the writing. The split is the point: events arrive in
    bursts and writing on each one would be a message flood, so state is cheap to
    update and only the timer decides when the chat hears about it.
    """

    started: float
    note: str = "Working on it"
    step: int = 0
    tool_calls: int = 0
    #: The tool currently running, if any.
    tool: str = ""
    #: Number of edits made so far, which drives the pulse frame.
    ticks: int = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def render(self) -> str:
        """One or two lines. Always different from the last render, because the
        elapsed time is in it — Telegram rejects an edit that changes nothing."""
        facts = []
        if self.step:
            facts.append(f"step {self.step}")
        if self.tool_calls:
            facts.append(f"{self.tool_calls} tool call{'' if self.tool_calls == 1 else 's'}")
        tail = " · ".join(facts)
        head = f"{_PULSE[self.ticks % len(_PULSE)]} {self.note}… {_duration(self.elapsed)}"
        if self.tool:
            return head + f"\n→ `{self.tool}`" + (f" · {tail}" if tail else "")
        return head + (f" · {tail}" if tail else "")


@dataclass(slots=True, frozen=True)
class _ChatWriter:
    """The chat operations a status message needs, already bound to one chat.

    Passing these in rather than the bridge itself keeps the status message from
    knowing anything about commands, permissions, or Telethon: it writes text and
    is told whether that worked.
    """

    #: ``send(text, reply_to=…) -> ids``
    send: Callable[..., Awaitable[list[int]]]
    #: ``edit(message_id, text) -> did it work``
    edit: Callable[..., Awaitable[bool]]
    #: ``reply(text)`` — chunked, as a fresh message; the fallback path.
    reply: Callable[..., Awaitable[None]]
    #: Characters per message.
    limit: int
    #: The command to answer in thread, if any.
    reply_to: int | None = None
    #: Only used to name the ticker task.
    chat_id: int = 0


class _StatusMessage:
    """The one message in the chat that tracks a run from start to answer.

    Sent as soon as the command is accepted, edited on a fixed interval while the
    run is in flight, and finally edited into the answer. Every part of it is
    cosmetic by construction: a chat that will not accept the message, or will not
    let it be edited, degrades to the plain behaviour of sending the answer when
    it is ready, and the run never notices.
    """

    def __init__(self, writer: _ChatWriter, *, interval: float, enabled: bool) -> None:
        self._writer = writer
        self._interval = interval
        self._enabled = enabled
        self._progress = _Progress(started=time.monotonic())
        self._message_id: int | None = None
        self._ticker: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ lifecycle ---
    async def open(self, note: str = "") -> None:
        """Acknowledge the command, and start editing that acknowledgement."""
        if not self._enabled:
            return
        if note:
            self._progress.note = note
        try:
            ids = await self._writer.send(self._progress.render(), reply_to=self._writer.reply_to)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the run matters, the placeholder does not
            log.debug("control.status_open_failed", error=str(exc))
            return
        if not ids:
            return
        self._message_id = ids[0]
        self._ticker = asyncio.create_task(
            self._tick(), name=f"control-status-{self._writer.chat_id}"
        )

    async def close(self) -> None:
        """Stop ticking, and wait for an edit already in flight to land.

        Waiting is not tidiness: an edit that is mid-flight when the ticker is
        cancelled could otherwise land *after* the final text and leave the run
        looking like it never finished.
        """
        ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    async def finish(self, text: str) -> None:
        """Put *text* where the status was, however that has to happen."""
        await self.close()
        chunks = _chunk(text, self._writer.limit)
        if not await self._write(chunks[0]):
            # No status message to edit — either it was never sent or the chat
            # refused the edit. The answer is what matters, so send it plainly.
            await self._writer.reply(text)
            return
        for chunk in chunks[1:]:
            await self._writer.send(chunk, reply_to=None)

    # -------------------------------------------------------------- updating --
    def set_note(self, note: str) -> None:
        """Change the headline. Takes effect on the next tick, not immediately."""
        self._progress.note = note

    def observe(self, event: AgentEvent) -> None:
        """Fold one runtime event into the progress state. Never does I/O."""
        progress = self._progress
        match event.kind:
            case EventKind.STEP_STARTED:
                progress.step = _int_or_none(event.data.get("step")) or progress.step + 1
                progress.note = "Thinking"
                progress.tool = ""
            case EventKind.TOOL_CALL_STARTED:
                progress.tool = str(event.data.get("tool") or "")
                progress.tool_calls += 1
                progress.note = "Working on it"
            case EventKind.TOOL_CALL_FINISHED:
                progress.tool = ""
            case EventKind.THINKING_DELTA:
                progress.note = "Thinking"
            case EventKind.TEXT_DELTA | EventKind.ASSISTANT_MESSAGE:
                progress.note = "Writing the answer"
            case EventKind.CONTEXT_COMPACTED:
                progress.note = "Compacting the conversation"
            case _:
                pass

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._progress.ticks += 1
            if not await self._write(self._progress.render()):
                return  # the message is gone; stop chasing it

    async def _write(self, text: str) -> bool:
        if self._message_id is None:
            return False
        if await self._writer.edit(self._message_id, text):
            return True
        self._message_id = None
        return False


# ------------------------------------------------------------------ bridge ----
@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[None]
    cancel: asyncio.Event
    #: The run's status message, so a confirmation prompt can say so in it.
    status: _StatusMessage | None = None


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
        watcher: AutoReplyWatcher | None = None,
        admin: RuntimeAdmin | None = None,
    ) -> None:
        self._manager = manager
        self._runtime_factory = runtime_factory
        self._settings = settings
        self._me_id = me_id
        self._audit = audit
        self._log_arguments = log_arguments
        #: Answers other people's messages under a standing instruction. Absent
        #: unless the deployment turned it on; see :mod:`tgagent.interfaces.autoreply`.
        self._watcher = watcher
        #: Policy and model settings, changeable by the owner from a chat.
        self._admin = admin

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
        #: When this bridge came up, for what ``ping`` reports.
        self._since = time.monotonic()

    # ---------------------------------------------------------- lifecycle ----
    async def start(self) -> None:
        """Register the Telethon event handler."""
        from telethon import events

        client = self._manager.client
        if self._me_id is None:
            self._me_id = getattr(self._manager.me, "id", None)
        if self._watcher is not None and self._watcher.me_id is None:
            # Only knowable now: the account is not identified until it connects.
            self._watcher.me_id = self._me_id

        self._handler = self._on_new_message
        client.add_event_handler(self._handler, events.NewMessage())
        log.info(
            "control.listening",
            trigger=self._settings.trigger,
            respond_to_self=self._settings.respond_to_self,
            allowed_senders=len(self._settings.allowed_senders),
            autoreply=bool(self._watcher and self._watcher.enabled),
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
            # Not a command — but it may be a message this account has been asked
            # to answer on the owner's behalf. Nothing in it is ever treated as an
            # instruction; see :mod:`tgagent.interfaces.autoreply`.
            return await self._maybe_autoreply(event, chat_id, message_id, text)

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
        # A trailing question mark is punctuation on "ping?" — but "?" on its own
        # is the help word, so stripping must never empty the instruction.
        word = word.rstrip("?") or word

        # Answered before the "one run per chat" check on purpose: asking whether
        # the bridge is alive is most useful while something is occupying it.
        if word in _PING_WORDS:
            await self._pong(source)
            return True

        # Same reasoning as ping, and more so: the kill switch for "the account is
        # answering people for me" must not depend on a model being reachable.
        if word in _WATCH_WORDS:
            await self._reply(source, await self._render_watches())
            return True

        if word in _UNWATCH_WORDS:
            await self._reply(source, await self._stop_watches())
            return True

        # Administration: policy, model settings, and flight mode. Handled by
        # prefix rather than exact match because these take arguments, and by the
        # bridge rather than a tool because a tool is reachable by content.
        head, _, argument = source.instruction.strip().partition(" ")
        head = head.casefold().rstrip(":,")
        if head in _ADMIN_WORDS:
            await self._administer(source, head, argument.strip())
            return True
        if head in _FLIGHT_WORDS:
            await self._reply(source, await self._flight(argument.strip()))
            return True

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
            body = _HELP_TEXT
            if self._watcher is not None and self._watcher.enabled:
                body += _HELP_AUTOREPLY
            if self._admin is not None and source.from_self:
                body += _HELP_ADMIN
            await self._reply(source, (body + _HELP_FOOTER).format(trigger=self._settings.trigger))
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
    def _status_for(self, source: CommandSource) -> _StatusMessage:
        """The status message for a run in *source*'s chat.

        Built even when ``progress_updates`` is off: a disabled status message
        sends nothing and its ``finish`` falls straight through to a plain reply,
        so the run has one path rather than two.
        """
        return _StatusMessage(
            _ChatWriter(
                send=partial(self._send, source),
                edit=partial(self._edit, source),
                reply=partial(self._reply, source),
                limit=self._settings.max_reply_chars,
                reply_to=self._reply_target(source),
                chat_id=source.chat_id,
            ),
            interval=self._settings.progress_interval,
            enabled=self._settings.progress_updates,
        )

    async def _run_command(self, source: CommandSource, cancel: asyncio.Event) -> None:
        token = _active_source.set(source)
        status = self._status_for(source)
        # Reachable from ask_in_chat, which has the source but not the run.
        if (run := self._active.get(source.key)) is not None:
            run.status = status
        try:
            # Acknowledged before queueing, not after: the whole reason this
            # message exists is that waiting is when you doubt it is listening.
            await status.open("Queued" if self._semaphore.locked() else "Working on it")
            async with self._semaphore:
                if cancel.is_set():
                    await status.finish("Cancelled.")
                    return
                status.set_note("Working on it")
                await self._execute(source, cancel, status)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await status.finish("Cancelled.")
            raise
        # The operator gets told what broke; the bridge keeps listening.
        except Exception as exc:
            log.error("control.run_failed", chat=source.chat_id, error=str(exc), exc_info=True)
            with contextlib.suppress(Exception):
                await status.finish(f"⚠️ That failed: {exc}")
        finally:
            with contextlib.suppress(Exception):
                await status.close()
            _active_source.reset(token)
            self.confirmations.reset()
            self._active.pop(source.key, None)

    async def _execute(
        self, source: CommandSource, cancel: asyncio.Event, status: _StatusMessage
    ) -> None:
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
                # A closure, not a bound method: the status message belongs to
                # this run, and concurrent runs in other chats have their own.
                on_event=lambda event: self._on_agent_event(event, status),
            )

        self._conversations[self._conversation_key(source)] = result.conversation_id
        await status.finish(_format_answer(result))
        log.info(
            "control.run_finished",
            chat=source.chat_id,
            steps=result.steps,
            tool_calls=result.tool_calls,
            stopped_because=result.stopped_because,
        )

    # --------------------------------------------------------- autoreply ------
    async def _maybe_autoreply(self, event: Any, chat_id: int, message_id: int, text: str) -> bool:
        """Answer an arriving message if a watch says to. Returns True if a run started.

        On the hot path — every message in every chat reaches here — so the cheap
        local checks come first and the database is only asked about chats that
        got past them.
        """
        watcher = self._watcher
        if watcher is None or not watcher.enabled:
            return False
        # The owner's own messages are commands, and the bridge's own replies are
        # outgoing too: answering either is how a machine talks to itself forever.
        if _is_outgoing(event):
            return False

        incoming = IncomingMessage(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            sender_id=_sender_id_of(event),
            sender_name=_display_name(getattr(event, "sender", None)),
            chat_title=_chat_title(event, getattr(event, "chat", None)),
            chat_kind=_chat_kind(event),
            date=getattr(getattr(event, "message", None), "date", None),
        )
        watch = await watcher.match(incoming)
        if watch is None:
            return False

        if (refusal := watcher.refuse(watch)) is not None:
            # Transient: a burst of messages, or the hourly ceiling. The next
            # message is considered again, and a run already in flight will see
            # this one in the history anyway.
            log.info("autoreply.deferred", chat=chat_id, watch=watch.id, reason=refusal)
            return False

        source = CommandSource(
            chat_id=chat_id,
            message_id=message_id,
            instruction=watch.instruction,
            chat_title=incoming.chat_title,
            chat_kind=incoming.chat_kind,
            sender_id=incoming.sender_id,
            sender_name=incoming.sender_name,
            date=incoming.date,
            peer=await _input_peer(event),
        )
        if source.key in self._active:
            log.info("autoreply.busy", chat=chat_id, watch=watch.id)
            return False

        cancel = asyncio.Event()
        task = asyncio.create_task(
            self._run_autoreply(source, watch, incoming, cancel), name=f"autoreply-{chat_id}"
        )
        # The same slot commands use, so one chat runs one thing at a time and
        # `agent stop` cancels an automatic reply exactly like anything else.
        self._active[source.key] = _ActiveRun(task=task, cancel=cancel)
        return True

    async def _run_autoreply(
        self,
        source: CommandSource,
        watch: ChatWatch,
        incoming: IncomingMessage,
        cancel: asyncio.Event,
    ) -> None:
        """Write and send one reply, or nothing at all.

        Nothing about this run is ever narrated into the chat: no status message,
        no error, no "cancelled". The person on the other end is not the operator,
        and showing them the machinery would be both a leak and a lie about who
        they are talking to. Failures go to the log and the audit trail.
        """
        watcher = self._watcher
        if watcher is None:  # pragma: no cover - only reachable if stopped mid-flight
            return
        try:
            async with self._semaphore:
                if cancel.is_set():
                    return
                runtime: AgentRuntime = self._runtime_factory()
                conversation_id = str(
                    watch.metadata.get("conversation_id") or f"tg-autoreply-{source.chat_id}"
                )
                log.info(
                    "autoreply.run_started",
                    chat=source.chat_id,
                    watch=watch.id,
                    conversation=conversation_id,
                )
                async with self._typing(source, enabled=watcher.settings.typing_indicator):
                    result: RunResult = await runtime.run(
                        watcher.build_prompt(watch, incoming),
                        conversation_id=conversation_id,
                        # Nobody to ask: a confirmation here would be asked of the
                        # person being replied to. CONFIRM therefore falls to
                        # permissions.non_interactive_decision, which is deny.
                        interactive=False,
                        cancel=cancel,
                    )

                reply = watcher.render_reply(result.answer, limit=self._settings.max_reply_chars)
                if reply is None:
                    log.info(
                        "autoreply.said_nothing",
                        chat=source.chat_id,
                        watch=watch.id,
                        stopped_because=result.stopped_because,
                    )
                    await self._record_autoreply(source, watch, decision="skip", error=None)
                    return

                # In a group a reply belongs in thread; in a private chat nobody
                # quotes the message they are answering.
                reply_to = source.message_id if source.chat_kind != "private chat" else None
                await self._send(source, reply, reply_to=reply_to)
                await watcher.record_reply(watch)
                await self._record_autoreply(source, watch, decision="allow", error=None)
                log.info(
                    "autoreply.replied",
                    chat=source.chat_id,
                    watch=watch.id,
                    replies=watch.reply_count,
                    remaining=max(0, watch.max_replies - watch.reply_count),
                    chars=len(reply),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "autoreply.failed",
                chat=source.chat_id,
                watch=watch.id,
                error=str(exc),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self._record_autoreply(source, watch, decision="error", error=str(exc))
        finally:
            self._active.pop(source.key, None)

    # ------------------------------------------------------------ administer --
    async def _administer(self, source: CommandSource, head: str, argument: str) -> None:
        """Run a ``policy`` or ``llm`` command, owner-only.

        The authorship check is stricter here than anywhere else in the bridge.
        ``control.allowed_senders`` grants somebody the ability to spend your tokens
        and act as your account; it does not extend to rewriting your permission
        policy or pointing the model at an endpoint of their choosing. The second
        one hands them every message the agent processes, and the two failures are
        not comparable in size.
        """
        if self._admin is None:
            await self._reply(
                source,
                "This listener was started without an administration surface, so I "
                "cannot change settings from here.",
            )
            return
        if not source.from_self:
            log.warning(
                "control.admin_refused", chat=source.chat_id, sender=source.sender_id, command=head
            )
            await self._reply(
                source,
                "Only the account owner can change the policy or the model, and this "
                "message is not from them.",
            )
            return
        # A policy or provider change mid-run would let a run observe its own rules
        # changing underneath it, which the engine's design deliberately rules out.
        if self._active:
            await self._reply(
                source,
                f"Something is still running. Wait for it, or send "
                f"`{self._settings.trigger} stop`, then try again.",
            )
            return

        try:
            result = (
                self._admin.policy(argument)
                if head in ("policy", "permissions")
                else self._admin.llm(argument)
            )
        except TgAgentError as exc:
            await self._reply(source, f"❌ {exc.user_message}")
            return

        # Deleting first: the reply is what draws the eye back to the chat, and a
        # key should be gone before anybody looks. Best effort — a chat that
        # refuses the deletion still gets the warning in the reply.
        if result.contained_secret:
            await self._delete_own(source)
        await self._reply(source, result.message)
        if result.changed:
            await self._record(
                source,
                decision="allow",
                error=None,
                method=f"control.{head}",
                risk=RiskTier.ACCOUNT_SECURITY,
            )

    async def _delete_own(self, source: CommandSource) -> None:
        """Remove the operator's own command, when it carried a credential.

        An API key pasted into a chat is in Telegram's history until somebody
        removes it, and "somebody" should not have to be the person who was trying
        to get on with something else.
        """
        client = self._manager.client
        deleter = getattr(client, "delete_messages", None)
        if deleter is None:
            return
        try:
            # `message_ids` by keyword: Telethon accepts either, and the argument
            # naming itself is what makes this unambiguous at the call site.
            await deleter(source.destination, message_ids=[source.message_id], revoke=True)
            log.info("control.secret_message_deleted", chat=source.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the warning in the reply still stands
            log.warning("control.secret_message_not_deleted", chat=source.chat_id, error=str(exc))

    # ---------------------------------------------------------- flight mode ---
    async def _flight(self, argument: str) -> str:
        """``agent flight on [hours] [instruction]`` / ``agent flight off``.

        One command for the situation the whole autoreply feature exists for: you
        are about to be unreachable, you have thirty seconds, and you do not want
        to explain per chat. It answers *private* chats only — replying for you in
        a group is a different and much worse idea — and it is answered without the
        model, because you are boarding.
        """
        watcher = self._watcher
        trigger = self._settings.trigger
        if watcher is None or not watcher.enabled or watcher.repository is None:
            return (
                "Automatic replies are switched off, so flight mode would do nothing. "
                "Set `TGAGENT_AUTOREPLY__ENABLED=true` and restart the listener."
            )

        words = argument.split(maxsplit=1)
        head = words[0].casefold() if words else ""
        if head in ("off", "stop", "end", "land", "landed"):
            stopped = await watcher.stop_flight_mode()
            return (
                "✈️ Flight mode off. I am not answering anyone for you."
                if stopped
                else "Flight mode was not on."
            )
        if head in ("", "status", "on", "start"):
            if head in ("", "status"):
                if (existing := await watcher.flight_mode()) is None:
                    return (
                        f"✈️ Flight mode is off.\n"
                        f"`{trigger} flight on` · `{trigger} flight on 3` for three hours\n"
                        f"`{trigger} flight on 3 tell them I'll answer when I land`"
                    )
                info = describe_watch(existing)
                left = info["minutes_left"]
                return (
                    f"✈️ **Flight mode is on** — answering private chats as you.\n"
                    f"· {info['replies_sent']}/{existing.max_replies} replies"
                    + (f", {left} min left" if left is not None else "")
                    + f"\n· “{existing.instruction[:200]}”\n\n"
                    f"`{trigger} flight off` to stop."
                )
            argument = words[1] if len(words) > 1 else ""

        hours, instruction = _split_hours(argument)
        watch = await watcher.start_flight_mode(hours=hours, instruction=instruction)
        info = describe_watch(watch)
        return (
            f"✈️ **Flight mode on.** I will answer private chats as you for "
            f"{_duration((watch.expires_at - datetime.now(UTC)).total_seconds()) if watch.expires_at else 'a while'}"
            f", up to {watch.max_replies} replies.\n"
            f"· “{watch.instruction[:200]}”\n"
            f"· groups and channels are left alone\n"
            f"· nothing I send is marked as automatic"
            + (f"\n· expires {info['expires_at']}" if info["expires_at"] else "")
            + f"\n\n`{trigger} flight off` when you land · `{trigger} watches` to check."
        )

    async def _render_watches(self) -> str:
        """The answer to ``agent watches``."""
        watcher = self._watcher
        if watcher is None or not watcher.enabled or watcher.repository is None:
            return (
                "Automatic replies are switched off. To use them, set "
                "`TGAGENT_AUTOREPLY__ENABLED=true` and restart the listener — then ask "
                "me to reply to someone for you."
            )
        watches = await watcher.repository.list_all(enabled_only=True)
        if not watches:
            return "I am not answering any chats for you."

        now = datetime.now(UTC)
        lines = ["**Answering for you**"]
        for watch in watches:
            info = describe_watch(watch, now=now)
            left = (
                f"{info['minutes_left']} min left"
                if info["minutes_left"] is not None
                else "no expiry"
            )
            lines.append(
                f"· **{info['chat']}** — {info['replies_sent']}/{watch.max_replies} replies, {left}"
            )
            lines.append(f"  “{watch.instruction[:200]}”")
        lines.append(f"\nSend `{self._settings.trigger} unwatch` to stop all of them.")
        return "\n".join(lines)

    async def _stop_watches(self) -> str:
        """The answer to ``agent unwatch`` — the kill switch."""
        watcher = self._watcher
        if watcher is None or watcher.repository is None:
            return "Automatic replies are switched off, so nothing was running."
        stopped = await watcher.repository.disable_all(reason=STOPPED_BY_OPERATOR)
        log.info("autoreply.stopped_all", stopped=stopped)
        if not stopped:
            return "I was not answering any chats for you."
        return f"Stopped answering {stopped} chat{'' if stopped == 1 else 's'}."

    def _on_agent_event(self, event: AgentEvent, status: _StatusMessage) -> None:
        # Events fold into the status message, which is rewritten on a timer —
        # the bridge never narrates a tool call as its own message. Every one
        # would be an outgoing message, which is both noisy and the thing the
        # loop breaker exists to bound. The detail goes to the log.
        status.observe(event)
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
        status = run.status if (run := self._active.get(source.key)) is not None else None
        try:
            await self._reply(
                source,
                f"⚠️ **Confirmation needed** ({request.risk.value})\n"
                f"```\n{request.render()}\n```\n"
                f"Reply `yes` to allow or `no` to refuse.",
            )
            # Otherwise the status message goes on claiming to be working, when
            # what it is actually doing is waiting for the operator.
            if status is not None:
                status.set_note("Waiting for your `yes` or `no`")
            approved = await future
        finally:
            if status is not None:
                status.set_note("Working on it")
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

    # ------------------------------------------------------------- liveness ---
    async def _pong(self, source: CommandSource) -> None:
        """Answer ``ping`` with numbers, without going anywhere near the model.

        Deliberately self-contained. It proves the process is running, that
        Telegram is reachable *from here*, and how far behind the listener is —
        none of which should cost tokens to find out, or stop working on the day
        the LLM is what is broken.
        """
        lag = _delivery_lag(source.date)
        started = time.perf_counter()
        ids = await self._send(source, "🏓 …", reply_to=self._reply_target(source))
        round_trip = (time.perf_counter() - started) * 1000

        lines = ["🏓 **pong**", f"send round-trip: `{round_trip:.0f} ms`"]
        if lag is not None:
            # Telegram stamps the command, this host reads the clock, so a wrong
            # clock here shows up as lag. Worth knowing either way.
            reached = f"{lag * 1000:.0f} ms" if lag < 1 else _duration(lag)
            lines.append(f"command reached me in: `{reached}`")
        lines.append(f"listening for: `{_duration(time.monotonic() - self._since)}`")
        lines.append(
            f"runs in flight: `{len(self._active)}` · commands this minute: "
            f"`{len(self._accepted)}/{self._settings.max_commands_per_minute}`"
        )
        text = "\n".join(lines)

        log.info(
            "control.ping",
            chat=source.chat_id,
            round_trip_ms=round(round_trip, 1),
            lag_ms=round(lag * 1000, 1) if lag is not None else None,
            active_runs=len(self._active),
        )
        if not (ids and await self._edit(source, ids[0], text)):
            await self._reply(source, text)

    # --------------------------------------------------------------- io ------
    async def _reply(self, source: CommandSource, text: str) -> None:
        """Deliver text to the chat, split across Telegram's length limit."""
        reply_to = self._reply_target(source)
        for index, chunk in enumerate(_chunk(text, self._settings.max_reply_chars)):
            await self._send(source, chunk, reply_to=reply_to if index == 0 else None)

    def _reply_target(self, source: CommandSource) -> int | None:
        """The message to answer in thread, if answering in thread at all."""
        return source.message_id if self._settings.reply_to_command else None

    async def _send(self, source: CommandSource, text: str, *, reply_to: int | None) -> list[int]:
        """Send one message and remember its id. Returns the ids it was given.

        Sent through the client rather than the gateway on purpose: this is the
        control plane answering its operator, not the agent acting on the
        account. Routing it through the permission engine would make every reply
        an ``EXTERNALLY_VISIBLE`` write needing confirmation — and the
        confirmation would be delivered by the very call being confirmed.
        """
        sent = await self._manager.client.send_message(
            source.destination, text, reply_to=reply_to, link_preview=False
        )
        ids = _sent_ids(sent)
        # Remembered so the bridge's own output can never be read back as a
        # command — the status message is as much a command-shaped risk as any
        # other outgoing message.
        self._own_messages.extend((source.chat_id, message_id) for message_id in ids)
        return ids

    async def _edit(self, source: CommandSource, message_id: int, text: str) -> bool:
        """Rewrite one of our own messages. False if the chat would not have it.

        Failure is ordinary here — the message may have been deleted, the edit
        window may have closed, or the text may be unchanged — and it is never
        worth failing a run over, so callers get a boolean rather than an
        exception and fall back to sending.
        """
        editor = getattr(self._manager.client, "edit_message", None)
        if editor is None:
            return False
        try:
            await editor(source.destination, message=message_id, text=text, link_preview=False)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.debug("control.edit_failed", chat=source.chat_id, error=str(exc))
            return False

    @contextlib.asynccontextmanager
    async def _typing(
        self, source: CommandSource, *, enabled: bool | None = None
    ) -> AsyncIterator[None]:
        """Show "typing…" for the duration of a run, if the client supports it.

        The indicator is cosmetic, so a client that cannot show one, or fails
        trying, must not affect the run. What it equally must not do is interfere
        with an exception coming *out* of the body — hence the single ``yield`` on
        every path. Yielding a second time in an error handler turns whatever the
        run raised into an opaque "generator didn't stop after athrow()".
        """
        action = getattr(self._manager.client, "action", None)
        entered: Any = None
        wanted = self._settings.typing_indicator if enabled is None else enabled
        if wanted and action is not None:
            try:
                entered = action(source.destination, "typing")
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

    async def _record(
        self,
        source: CommandSource,
        *,
        decision: str,
        error: str | None,
        method: str = "control.command",
        risk: RiskTier = RiskTier.READ_ONLY,
    ) -> None:
        if self._audit is None:
            return
        entry = AuditEntry(
            run_id="control",
            conversation_id=self._conversations.get(source.key),
            method=method,
            risk=risk.value,
            decision=decision,
            target=f"chat/{source.chat_id}",
            argument_digest=hashlib.sha256(source.instruction.encode()).hexdigest()[:16],
            argument_preview=source.instruction[:500] if self._log_arguments else None,
            succeeded=decision == "allow",
            error=error,
            origin="control",
        )
        await self._write_audit(entry)

    async def _record_autoreply(
        self, source: CommandSource, watch: ChatWatch, *, decision: str, error: str | None
    ) -> None:
        """Audit one automatic reply.

        Recorded at ``externally_visible`` whatever the outcome, and with its own
        origin, because this is the one path where the account speaks to somebody
        else without a per-message confirmation: "what did it say, to whom, under
        which instruction" has to be answerable afterwards.
        """
        if self._audit is None:
            return
        await self._write_audit(
            AuditEntry(
                run_id=f"autoreply:{watch.id[:8]}",
                conversation_id=str(watch.metadata.get("conversation_id") or ""),
                method="autoreply.reply",
                risk=RiskTier.EXTERNALLY_VISIBLE.value,
                decision=decision,
                target=f"chat/{source.chat_id}",
                argument_digest=hashlib.sha256(watch.instruction.encode()).hexdigest()[:16],
                argument_preview=watch.instruction[:500] if self._log_arguments else None,
                succeeded=decision == "allow",
                error=error,
                origin="autoreply",
            )
        )

    async def _write_audit(self, entry: AuditEntry) -> None:
        if self._audit is None:
            return
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
            peer=await _input_peer(event),
        )


# ---------------------------------------------------------------- helpers -----
def _format_answer(result: RunResult) -> str:
    answer = result.answer.strip() or "(no answer)"
    if result.cancelled:
        return "Cancelled."
    if result.stopped_because:
        # Telethon's markdown italicises on __double__ underscores; a single one
        # is left in the message as a literal character.
        answer += f"\n\n__stopped: {result.stopped_because}__"
    if result.errors:
        answer += "\n" + "\n".join(f"__! {message}__" for message in result.errors[:3])
    return answer


def _duration(seconds: float) -> str:
    """A compact, human duration: ``8s``, ``1m 05s``, ``2h 07m``."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _split_hours(argument: str) -> tuple[float | None, str]:
    """Read a leading duration off ``"3 tell them I'll answer later"``.

    Accepts ``3``, ``3h``, ``90m``, or nothing at all; whatever follows is the
    instruction. Typing a number first is what people do, so it is what this
    understands.
    """
    words = argument.split(maxsplit=1)
    if not words:
        return None, ""
    token = words[0].casefold()
    rest = words[1].strip() if len(words) > 1 else ""
    number, unit = (token[:-1], token[-1]) if token[-1:] in ("h", "m") else (token, "h")
    try:
        value = float(number)
    except ValueError:
        return None, argument.strip()
    if value <= 0:
        return None, rest
    return (value / 60 if unit == "m" else value), rest


def _delivery_lag(date: datetime | None) -> float | None:
    """Seconds between Telegram stamping the command and the bridge reading it.

    Clamped at zero: the stamp comes from Telegram's clock and the comparison
    from this host's, so a host running slightly ahead would otherwise report a
    command that arrived before it was sent.
    """
    if date is None:
        return None
    stamped = date if date.tzinfo is not None else date.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - stamped).total_seconds())


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


def _sender_id_of(event: Any) -> int | None:
    return _int_or_none(getattr(event, "sender_id", None)) or _int_or_none(
        getattr(getattr(event, "message", None), "sender_id", None)
    )


def _is_outgoing(event: Any) -> bool:
    """Whether this account sent the message — the owner, or the bridge itself."""
    message = getattr(event, "message", None) or event
    return bool(getattr(message, "out", False))


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


async def _input_peer(event: Any) -> Any:
    """The event's chat as a peer Telethon can address, or ``None``.

    Tried in order of cost: the cached ``input_chat`` property, then
    ``get_input_chat()``, which may go to the network. ``None`` on failure leaves
    :attr:`CommandSource.destination` falling back to the raw id — no worse than
    before, and still correct for a chat the session has cached.
    """
    cached = getattr(event, "input_chat", None)
    if cached is not None:
        return cached
    getter = getattr(event, "get_input_chat", None)
    if getter is None:
        return None
    try:
        return await getter()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the id fallback is still worth trying
        log.debug("control.input_peer_unavailable", error=str(exc))
        return None


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
