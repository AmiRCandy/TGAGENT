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


@dataclass(slots=True)
class HistoryPage:
    """One page of messages, plus everything needed to fetch the next."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    #: Pass back as ``offset_id`` to continue. ``None`` when exhausted.
    next_offset_id: int | None = None
    has_more: bool = False
    total_available: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "messages": self.messages,
            "count": len(self.messages),
            "has_more": self.has_more,
        }
        if self.next_offset_id is not None:
            out["next_offset_id"] = self.next_offset_id
        if self.total_available is not None:
            out["total_available"] = self.total_available
        return out


class HistoryReader:
    """Paginated, filtered history access built on the gateway."""

    def __init__(self, gateway: TelegramGateway) -> None:
        self._gateway = gateway

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
        min_date: str | None = None,
        max_date: str | None = None,
        context: CallContext | None = None,
    ) -> HistoryPage:
        """Search across every chat the account can see.

        Uses the raw ``messages.SearchGlobal`` request because the friendly
        layer has no equivalent — a good example of why raw TL access matters.
        """
        page_size = _clamp(limit)
        arguments: dict[str, Any] = {
            "q": query,
            "filter": {"_": "InputMessagesFilterEmpty"},
            "min_date": _epoch(min_date),
            "max_date": _epoch(max_date),
            "offset_rate": 0,
            "offset_peer": {"_": "InputPeerEmpty"},
            "offset_id": offset_id,
            "limit": page_size,
        }
        result = await self._gateway.call(
            "messages.SearchGlobal",
            arguments,
            context=context,
            projector=_project_messages_container,
        )
        payload = result.payload if isinstance(result.payload, dict) else {}
        messages = payload.get("messages", [])
        page = _page_from(messages, page_size, reverse=False)
        page.total_available = payload.get("total")
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


def _project_messages_container(result: Any) -> dict[str, Any]:
    """Flatten a ``messages.Messages``-family response into a compact dict."""
    return {
        "messages": [message_to_dict(m) for m in getattr(result, "messages", None) or []],
        "total": getattr(result, "count", None),
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
