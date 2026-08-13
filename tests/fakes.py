"""Test doubles.

CI must never touch a real Telegram account or a real model provider, so
everything external has a fake here. The fakes are deliberately *behavioural*
rather than mock-based: ``FakeTelegramClient`` really returns message-shaped
objects and really raises Telethon's error types, so the code paths that matter
(serialisation, error translation, pagination) are genuinely exercised.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tgagent.security.confirm import ConfirmationOutcome, ConfirmationRequest


# ------------------------------------------------------------- telegram -----
class FakePeer:
    """Stands in for an InputPeer; identity is all the tests need."""

    def __init__(self, identifier: int | str) -> None:
        self.id = identifier
        self.user_id = identifier

    def __repr__(self) -> str:
        return f"FakePeer({self.id!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakePeer) and other.id == self.id

    def __hash__(self) -> int:
        return hash(("FakePeer", str(self.id)))


class FakeEntity:
    def __init__(
        self,
        identifier: int,
        *,
        username: str | None = None,
        first_name: str = "",
        title: str = "",
        broadcast: bool = False,
        megagroup: bool = False,
    ) -> None:
        self.id = identifier
        self.username = username
        self.first_name = first_name
        self.last_name = ""
        self.title = title
        self.broadcast = broadcast
        self.megagroup = megagroup
        self.bot = False
        self.phone = None


class FakeMessage:
    """Shaped like a Telethon ``Message`` for the fields the project reads."""

    def __init__(
        self,
        identifier: int,
        text: str = "",
        *,
        sender_id: int = 1,
        date: datetime | None = None,
        out: bool = False,
        media: Any = None,
        chat_id: int = -100123,
    ) -> None:
        self.id = identifier
        self.message = text
        self.date = date or datetime(2026, 1, 15, 12, 0, tzinfo=UTC) + timedelta(minutes=identifier)
        self.out = out
        self.media = media
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.from_id = None
        self.reply_to = None
        self.fwd_from = None
        self.edit_date = None
        self.pinned = False
        self.views = None
        self.reactions = None
        self.action = None


class FakeControlEvent:
    """Shaped like a Telethon ``NewMessage.Event`` for what the bridge reads.

    The bridge deliberately reads its event through ``getattr``, so this covers
    the whole surface it touches: text, ids, who sent it, and the replied-to
    message. Nothing here imports Telethon, which is what lets the control tests
    run offline.
    """

    def __init__(
        self,
        text: str,
        *,
        chat_id: int = -100123,
        message_id: int = 500,
        sender_id: int = 1,
        out: bool = False,
        sender: FakeEntity | None = None,
        chat: FakeEntity | None = None,
        reply_to_message: FakeMessage | None = None,
        is_private: bool = False,
        is_group: bool = True,
        is_channel: bool = False,
        date: datetime | None = None,
    ) -> None:
        self.raw_text = text
        self.text = text
        self.id = message_id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.is_private = is_private
        self.is_group = is_group
        self.is_channel = is_channel
        self.sender = sender or FakeEntity(sender_id, username="owner", first_name="Owner")
        self.chat = chat or FakeEntity(chat_id, title="Project X")
        self._reply = reply_to_message
        #: Telethon exposes the chat as a resolvable peer here; a bare id is not
        #: resolvable for an uncached user, so the bridge has to use this.
        self.input_chat = FakePeer(chat_id)

        self.message = FakeMessage(
            message_id, text, sender_id=sender_id, out=out, chat_id=chat_id, date=date
        )
        if reply_to_message is not None:
            self.message.reply_to = type("ReplyTo", (), {"reply_to_msg_id": reply_to_message.id})()

    async def get_reply_message(self) -> FakeMessage | None:
        return self._reply

    async def get_sender(self) -> FakeEntity:
        return self.sender

    async def get_input_chat(self) -> FakePeer:
        return FakePeer(self.chat_id)


class FakeDocument:
    def __init__(
        self,
        *,
        size: int = 1024,
        mime_type: str = "application/pdf",
        file_name: str = "report.pdf",
    ) -> None:
        self.id = 999
        self.size = size
        self.mime_type = mime_type
        self.attributes = [type("Attr", (), {"file_name": file_name})()]


class FakeMedia:
    def __init__(self, document: FakeDocument | None = None) -> None:
        self.document = document or FakeDocument()
        self.photo = None
        self.webpage = None
        self.poll = None
        self.geo = None
        self.phone_number = None


class FakeDialog:
    def __init__(
        self,
        identifier: int,
        name: str,
        *,
        unread: int = 0,
        is_user: bool = True,
        message: FakeMessage | None = None,
    ) -> None:
        self.id = identifier
        self.name = name
        self.unread_count = unread
        self.unread_mentions_count = 0
        self.is_user = is_user
        self.is_group = not is_user
        self.is_channel = False
        self.pinned = False
        self.archived = False
        self.entity = FakeEntity(identifier, username=name.lstrip("@") if is_user else None)
        self.message = message


class _NullAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class FakeTelegramClient:
    """A Telethon client stand-in that records what it was asked to do."""

    def __init__(self, *, messages: list[FakeMessage] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raw_calls: list[Any] = []
        self._connected = True
        self._authorized = True
        self.messages = messages or [
            FakeMessage(i, f"message {i}", sender_id=1 if i % 2 else 2) for i in range(1, 21)
        ]
        #: Set to an exception to make the next friendly call raise it.
        self.next_error: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        #: Text of every message this client sent, keyed by id and *with edits
        #: applied* — what the chat would be showing, rather than the history of
        #: how it got there. The control bridge edits one message repeatedly, so
        #: this is what its tests assert against.
        self.visible: dict[int, str] = {}
        self.downloads: list[str] = []
        self.handlers: list[tuple[Any, Any]] = []
        #: Ids of messages this client sent, in order. The control bridge keys its
        #: "never read my own output" guard on them, so tests need to see them.
        self.sent_ids: list[int] = []
        #: When True, a bare integer entity is refused the way Telethon refuses
        #: one it has no access_hash for. Off by default: most tests address
        #: chats by username, where an id is never involved.
        self.require_input_peer = False
        self._next_sent_id = 900

    # lifecycle -------------------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_user_authorized(self) -> bool:
        return self._authorized

    @property
    def disconnected(self) -> asyncio.Future[None]:
        return asyncio.get_event_loop().create_future()

    # event handlers --------------------------------------------------------
    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers = [(cb, ev) for cb, ev in self.handlers if cb is not callback]

    def action(self, entity: Any, action: str = "typing") -> Any:
        """Mimic Telethon's ``client.action`` async context manager."""
        self.calls.append(("action", {"entity": entity, "action": action}))
        return _NullAsyncContext()

    # entity resolution -----------------------------------------------------
    async def get_input_entity(self, peer: Any) -> FakePeer:
        self.calls.append(("get_input_entity", {"peer": peer}))
        if peer in ("@missing", "missing"):
            raise ValueError("No user has that username")
        return FakePeer(peer)

    async def get_entity(self, peer: Any) -> FakeEntity:
        self.calls.append(("get_entity", {"peer": peer}))
        if peer in ("@missing", "missing"):
            raise ValueError("No user has that username")
        name = str(peer).lstrip("@")
        numeric = name.lstrip("-").isdigit()
        return FakeEntity(
            # A numeric reference resolves to itself. Telethon does the same, and
            # a fake that mapped every peer to one id could not tell two chats
            # apart — which is precisely what anything multi-peer needs to test.
            int(name) if numeric else 12345,
            username=None if numeric else name,
            first_name=name.title(),
        )

    async def get_me(self) -> FakeEntity:
        return FakeEntity(1, username="owner", first_name="Owner")

    # reads -----------------------------------------------------------------
    async def get_messages(self, entity: Any = None, **kwargs: Any) -> list[FakeMessage]:
        self._maybe_raise()
        self.calls.append(("get_messages", {"entity": entity, **kwargs}))
        if ids := kwargs.get("ids"):
            wanted = set(ids if isinstance(ids, list) else [ids])
            return [m for m in self.messages if m.id in wanted]
        limit = int(kwargs.get("limit") or 10)
        rows = self.messages
        if search := kwargs.get("search"):
            rows = [m for m in rows if search.lower() in m.message.lower()]
        if kwargs.get("reverse"):
            return rows[:limit]
        return list(reversed(rows))[:limit]

    async def get_dialogs(self, **kwargs: Any) -> list[FakeDialog]:
        self._maybe_raise()
        self.calls.append(("get_dialogs", kwargs))
        return [
            FakeDialog(1, "@alex", unread=3, message=FakeMessage(20, "see you then")),
            FakeDialog(-100123, "Project X", unread=0, is_user=False),
            FakeDialog(2, "@john", unread=1),
        ][: int(kwargs.get("limit") or 10)]

    async def get_participants(self, entity: Any = None, **kwargs: Any) -> list[FakeEntity]:
        self.calls.append(("get_participants", {"entity": entity, **kwargs}))
        return [FakeEntity(1, username="alex"), FakeEntity(2, username="john")]

    # writes ----------------------------------------------------------------
    async def send_message(
        self, entity: Any = None, message: str = "", **kwargs: Any
    ) -> FakeMessage:
        self._maybe_raise()
        self._check_addressable(entity)
        self.calls.append(("send_message", {"entity": entity, "message": message, **kwargs}))
        self.sent.append({"entity": entity, "message": message})
        # Distinct ids matter to the control bridge, which remembers what it sent
        # so its own output can never be read back as a command.
        self._next_sent_id += 1
        self.sent_ids.append(self._next_sent_id)
        self.visible[self._next_sent_id] = message
        return FakeMessage(self._next_sent_id, message, out=True, chat_id=entity)

    async def edit_message(self, entity: Any = None, **kwargs: Any) -> FakeMessage:
        # Editing needs a resolvable peer exactly as sending does, so an id the
        # session cannot address must fail here too.
        self._check_addressable(entity)
        self.calls.append(("edit_message", {"entity": entity, **kwargs}))
        message_id = kwargs.get("message")
        text = str(kwargs.get("text", ""))
        if isinstance(message_id, int):
            self.visible[message_id] = text
        return FakeMessage(message_id if isinstance(message_id, int) else 1, text, out=True)

    async def delete_messages(self, entity: Any = None, **kwargs: Any) -> list[Any]:
        self._maybe_raise()
        self.calls.append(("delete_messages", {"entity": entity, **kwargs}))
        return [object()]

    async def forward_messages(self, entity: Any = None, **kwargs: Any) -> list[FakeMessage]:
        self.calls.append(("forward_messages", {"entity": entity, **kwargs}))
        return [FakeMessage(1000, "forwarded")]

    async def send_read_acknowledge(self, entity: Any = None, **kwargs: Any) -> bool:
        self.calls.append(("send_read_acknowledge", {"entity": entity, **kwargs}))
        return True

    async def download_media(self, message: Any, file: str | None = None, **kwargs: Any) -> str:
        self.calls.append(("download_media", {"file": file}))
        self.downloads.append(str(file))
        if file:
            from pathlib import Path

            path = Path(file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 fake content")
        return str(file)

    # raw TL ----------------------------------------------------------------
    async def __call__(self, request: Any) -> Any:
        self._maybe_raise()
        self.raw_calls.append(request)
        name = type(request).__name__
        if "Search" in name:
            return type(
                "Result",
                (),
                {
                    "messages": self.messages[:5],
                    "count": len(self.messages),
                    "chats": [],
                    "users": [],
                },
            )()
        return type("Result", (), {"ok": True, "request": name})()

    def _check_addressable(self, entity: Any) -> None:
        """Refuse a bare id, as Telethon does for an entity it cannot resolve."""
        if self.require_input_peer and isinstance(entity, int):
            raise ValueError(
                f"Could not find the input entity for PeerUser(user_id={entity}) "
                f"(PeerUser). Please read https://docs.telethon.dev/en/stable/"
                f"concepts/entities.html to find out more details."
            )

    def _maybe_raise(self) -> None:
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error


class FakeClientManager:
    """Stands in for :class:`~tgagent.telegram.client.TelegramClientManager`."""

    def __init__(self, client: FakeTelegramClient | None = None) -> None:
        self._client = client or FakeTelegramClient()
        self._session_path = "/tmp/fake.session"
        self.me = FakeEntity(1, username="owner", first_name="Owner")

    @property
    def client(self) -> FakeTelegramClient:
        return self._client

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    async def ensure_connected(self) -> None:
        if not self._client.is_connected():
            await self._client.connect()

    async def start(self, **_kwargs: Any) -> FakeTelegramClient:
        return self._client

    async def stop(self) -> None:
        await self._client.disconnect()


# --------------------------------------------------------- confirmations ----
@dataclass
class RecordingConfirmation:
    """A confirmation provider with a scripted answer, for policy tests."""

    approve: bool = True
    interactive: bool = True
    requests: list[ConfirmationRequest] = field(default_factory=list)
    #: Per-method overrides, e.g. ``{"send_message": False}``.
    answers: dict[str, bool] = field(default_factory=dict)

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        self.requests.append(request)
        approved = self.answers.get(request.method, self.approve)
        return ConfirmationOutcome(approved=approved, reason="scripted answer in tests")


# ---------------------------------------------------------------- misc ------
class CollectingEvents:
    """Collects runtime events so tests can assert on the sequence."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.events]

    def of(self, kind: Any) -> list[Any]:
        return [e for e in self.events if e.kind is kind]
