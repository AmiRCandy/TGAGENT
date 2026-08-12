"""Reading large histories without drowning in them.

A Telegram account can hold millions of messages. Every access here is
**cursor-paginated with a hard cap**, returns **compact projections**, and
prefers **server-side filtering** (``search=``, ``filter=``, ``offset_date=``)
over pulling everything back and filtering locally.

The cursor returned with each page is what lets the agent walk a long history
across several turns, or — far more efficiently — loop over it inside the
sandbox and return only what matters. That second path is the reason large
histories are tractable at all: filtering 5,000 messages to 12 costs one LLM
turn instead of fifty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tgagent.telegram.entities import parse_datetime
from tgagent.telegram.gateway import CallContext, TelegramGateway
from tgagent.telegram.serialize import dialog_to_dict, message_to_dict

#: Hard ceiling on one page, whatever the caller asks for. Keeps a single tool
#: result inside a sane token budget.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

#: How many global-search cursors a reader remembers. A handful of interleaved
#: searches is the realistic case; older ones are simply forgotten, which costs
#: a restart rather than correctness.
_CURSOR_MEMORY = 32


@dataclass(slots=True)
class HistoryPage:
    """One page of messages, plus everything needed to fetch the next."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Pass back as ``offset_id`` to continue. ``None`` when exhausted.
    next_offset_id: int | None = None
    has_more: bool = False
    total_available: int | None = None
    #: Global search only: the other two thirds of its cursor. ``offset_id`` is
    #: meaningless on its own there, because message ids are per-chat.
    next_offset_rate: int | None = None
    next_offset_peer: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "messages": self.messages,
            "count": len(self.messages),
            "has_more": self.has_more,
        }
        if self.next_offset_id is not None:
            out["next_offset_id"] = self.next_offset_id
        if self.next_offset_rate is not None:
            out["next_offset_rate"] = self.next_offset_rate
        if self.next_offset_peer is not None:
            out["next_offset_peer"] = self.next_offset_peer
        if self.total_available is not None:
            out["total_available"] = self.total_available
        return out


class HistoryReader:
    """Paginated, filtered history access built on the gateway."""

    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway
        self._global_cursors: dict[tuple[str, int], tuple[int, int | str]] = {}

    async def read(
        self,
        peer: str | int,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset_id: int = 0,
        offset_date: str | datetime | None = None,
        min_id: int = 0,
        max_id: int = 0,
        reverse: bool = False,
        search: str | None = None,
        from_user: str | int | None = None,
        media_filter: str | None = None,
        context: CallContext | None = None,
    ) -> HistoryPage:
        """Fetch one page of history.

        ``reverse=True`` reads oldest-first, which is what a chronological
        summary wants; the default reads newest-first, which is what "what did I
        miss" wants.
        """
        page_size = _clamp(limit)
        arguments: dict[str, Any] = {"entity": peer, "limit": page_size}

        if offset_id:
            arguments["offset_id"] = offset_id
        if offset_date is not None:
            arguments["offset_date"] = (
                offset_date if isinstance(offset_date, datetime) else parse_datetime(offset_date)
            )
        if min_id:
            arguments["min_id"] = min_id
        if max_id:
            arguments["max_id"] = max_id
        if reverse:
            arguments["reverse"] = True
        if search:
            arguments["search"] = search
        if from_user is not None:
            arguments["from_user"] = from_user
        if media_filter:
            arguments["filter"] = media_filter

        result = await self._gateway.call(
            "get_messages",
            arguments,
            context=context,
            projector=lambda messages: [message_to_dict(m) for m in messages],
        )
        messages = result.payload if isinstance(result.payload, list) else []
        return _page_from(messages, page_size, reverse=reverse)

    async def search_global(
        self,
        query: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset_id: int = 0,
        offset_rate: int = 0,
        offset_peer: int | str | None = None,
        min_date: str | None = None,
        max_date: str | None = None,
        context: CallContext | None = None,
    ) -> HistoryPage:
        """Search across every chat the account can see.

        Uses the raw ``messages.SearchGlobal`` request because the friendly
        layer has no equivalent — a good example of why raw TL access matters.

        Its cursor has three parts: the previous slice's ``next_rate``, the last
        message's peer, and its id. Supplying only the id restarts at the first
        page, so the other two are remembered here against the id they belong to
        — that keeps callers that thread a single offset (the tools do) correct.
        """
        page_size = _clamp(limit)
        search_key = f"{query}|{min_date or ''}|{max_date or ''}"
        rate, peer = int(offset_rate or 0), offset_peer
        if offset_id and not (rate and peer is not None):
            remembered = self._global_cursors.get((search_key, int(offset_id)))
            if remembered is not None:
                rate, peer = remembered

        arguments: dict[str, Any] = {
            "q": query,
            "filter": {"_": "InputMessagesFilterEmpty"},
            "min_date": _epoch(min_date),
            "max_date": _epoch(max_date),
            "offset_rate": rate,
            # InputPeerEmpty is the "start of results" sentinel.
            "offset_peer": peer if peer is not None else {"_": "InputPeerEmpty"},
            "offset_id": int(offset_id),
            "limit": page_size,
        }
        result = await self._gateway.call(
            "messages.SearchGlobal",
            arguments,
            context=context,
            projector=_project_messages_container,
        )
        payload = result.payload if isinstance(result.payload, dict) else {}
        page = _global_page_from(
            payload.get("messages", []), page_size, next_rate=payload.get("next_rate")
        )
        page.total_available = payload.get("total")
        self._remember_cursor(search_key, page)
        return page

    async def search_in_chat(
        self,
        peer: str | int,
        query: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset_id: int = 0,
        min_date: str | None = None,
        max_date: str | None = None,
        from_user: str | int | None = None,
        media_filter: str | None = None,
        context: CallContext | None = None,
    ) -> HistoryPage:
        """Search inside one chat, server-side.

        ``min_date`` has no direct equivalent in the friendly layer, so it is
        applied to the returned page rather than pushed to the server; the
        ``max_date`` bound *is* pushed down, as ``offset_date``.
        """
        page = await self.read(
            peer,
            limit=limit,
            offset_id=offset_id,
            search=query or None,
            from_user=from_user,
            media_filter=media_filter,
            offset_date=max_date,
            context=context,
        )
        if min_date:
            floor = parse_datetime(min_date)
            page.messages = [
                m
                for m in page.messages
                if not m.get("date") or parse_datetime(str(m["date"])) >= floor
            ]
        return page

    def _remember_cursor(self, search_key: str, page: HistoryPage) -> None:
        """Store the rate/peer halves of a global cursor against its offset id."""
        if page.next_offset_id is None or page.next_offset_peer is None:
            return
        self._global_cursors[(search_key, page.next_offset_id)] = (
            page.next_offset_rate or 0,
            page.next_offset_peer,
        )
        while len(self._global_cursors) > _CURSOR_MEMORY:
            self._global_cursors.pop(next(iter(self._global_cursors)))

    async def list_dialogs(
        self,
        *,
        limit: int = 50,
        archived: bool = False,
        only_unread: bool = False,
        context: CallContext | None = None,
    ) -> list[dict[str, Any]]:
        """List conversations, newest activity first."""
        result = await self._gateway.call(
            "get_dialogs",
            {"limit": _clamp(limit), "archived": archived},
            context=context,
            projector=lambda dialogs: [dialog_to_dict(d) for d in dialogs],
        )
        dialogs = result.payload if isinstance(result.payload, list) else []
        if only_unread:
            dialogs = [d for d in dialogs if d.get("unread_count", 0) > 0]
        return dialogs


def _clamp(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(MAX_PAGE_SIZE, value))


def _page_from(messages: list[Any], page_size: int, *, reverse: bool) -> HistoryPage:
    rows = [m for m in messages if isinstance(m, dict)]
    has_more = len(rows) >= page_size
    next_offset: int | None = None
    if has_more and rows:
        # Paging backwards keys off the oldest id seen; forwards off the newest.
        ids = [value for m in rows if isinstance(value := m.get("id"), int)]
        if ids:
            next_offset = max(ids) if reverse else min(ids)
    return HistoryPage(messages=rows, next_offset_id=next_offset, has_more=has_more)


def _global_page_from(messages: list[Any], page_size: int, *, next_rate: Any) -> HistoryPage:
    """Build a page for ``messages.searchGlobal``.

    The cursor is the *last* row of the slice together with its peer and the
    server's ``next_rate``; ids alone cannot order a result set that spans chats.
    Without a peer the cursor cannot advance, and advertising one that cannot
    advance makes a paginating caller re-read the same page forever, so the page
    reports itself exhausted instead.
    """
    rows = [m for m in messages if isinstance(m, dict)]
    page = HistoryPage(messages=rows)
    if len(rows) < page_size:
        return page

    last = rows[-1]
    last_id, last_peer = last.get("id"), last.get("chat_id")
    if not isinstance(last_id, int) or not isinstance(last_peer, (int, str)):
        return page

    page.has_more = True
    page.next_offset_id = last_id
    page.next_offset_peer = last_peer
    page.next_offset_rate = next_rate if isinstance(next_rate, int) else 0
    return page


def _project_messages_container(result: Any) -> dict[str, Any]:
    """Flatten a ``messages.Messages``-family response into a compact dict."""
    return {
        "messages": [message_to_dict(m) for m in getattr(result, "messages", None) or []],
        "total": getattr(result, "count", None),
        # The rate half of a global-search cursor; absent on a non-sliced response.
        "next_rate": getattr(result, "next_rate", None),
        "chats": [
            {"id": getattr(c, "id", None), "title": getattr(c, "title", None)}
            for c in (getattr(result, "chats", None) or [])[:100]
        ],
        "users": [
            {
                "id": getattr(u, "id", None),
                "username": getattr(u, "username", None),
                "first_name": getattr(u, "first_name", None),
            }
            for u in (getattr(result, "users", None) or [])[:100]
        ],
    }


def _epoch(value: str | None) -> int:
    """Telegram's raw search requests take Unix timestamps; 0 means unbounded."""
    if not value:
        return 0
    return int(parse_datetime(value).timestamp())
