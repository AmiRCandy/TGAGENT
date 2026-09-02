"""Tools for answering a chat automatically.

These are how "reply to Alex for me while I'm on the flight" becomes a record in
the database. The behaviour they set up — what fires, what stops it, and what the
resulting run is told — lives in :mod:`tgagent.interfaces.autoreply`, which is
also where the reasoning about why this is the most dangerous capability here is
written down.

Three tools, deliberately: start one, see what is running, stop it. There is no
tool for editing a watch in place — re-starting one on the same chat replaces it,
which is both simpler and easier to audit than a half-changed instruction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tgagent.errors import ToolInputError
from tgagent.interfaces.autoreply import STOPPED_BY_OPERATOR, describe_watch, ttl_for
from tgagent.risk import RiskTier
from tgagent.storage.models import ChatWatch
from tgagent.tools.base import (
    ToolContext,
    ToolResult,
    boolean_field,
    integer_field,
    object_schema,
    require,
    string_field,
)

_DISABLED = (
    "Automatic replies are switched off in this deployment. The account owner has "
    "to set TGAGENT_AUTOREPLY__ENABLED=true and restart the listener; it is off by "
    "default because it lets messages be sent as them without a confirmation. Tell "
    "them that rather than trying another way."
)


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _marked_chat_id(peer_id: int, kind: str) -> int:
    """A peer id in the form Telegram *events* use.

    Load-bearing, and easy to get wrong: ``telegram_resolve_peer`` reports the
    bare id (``1234567890``), while the id on an arriving message is marked by
    type (``-1001234567890`` for a channel or supergroup, ``-1234567890`` for a
    legacy group, unchanged for a user). A watch stored under the bare id simply
    never fires, silently, for every chat that is not a private one.
    """
    if peer_id < 0:  # already marked
        return peer_id
    if kind == "channel":
        return int(f"-100{peer_id}")
    if kind == "chat":
        return -peer_id
    return peer_id


class AutoReplyStartTool:
    name = "autoreply_start"
    description = (
        "Answer a chat automatically, as the account owner, until it expires — for "
        "'reply for me while I'm away'. The instruction is standing guidance for every "
        "reply: who to answer, what never to commit to, and when to stay silent. Read "
        "the recent history with that person first so it captures how they actually "
        "write. It stops at the expiry or the reply budget, whichever comes first; give "
        "the owner both numbers."
    )
    risk_hint = RiskTier.EXTERNALLY_VISIBLE
    parameters = object_schema(
        {
            "peer": string_field(
                "The chat to answer: an @username, or the numeric id of the chat the "
                "request was made in."
            ),
            "instruction": string_field(
                "Standing guidance for every reply, in the owner's words where they "
                "gave any. Include what to cover, what never to commit to, and when to "
                "say nothing."
            ),
            "senders": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "In a group, only answer these people (@username or id). Omit in a "
                    "private chat, where the other person is the only one who can write."
                ),
                "maxItems": 20,
            },
            "duration_minutes": integer_field(
                "How long to keep answering. Ask the owner if they did not say; the "
                "deployment default applies when omitted, and its ceiling always wins.",
                minimum=1,
            ),
            "max_replies": integer_field(
                "Stop after this many replies. Keep it to what the situation needs.",
                minimum=1,
            ),
        },
        required=["peer", "instruction"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings.autoreply
        if context.watches is None or not settings.enabled:
            return ToolResult.error(_DISABLED)

        gateway = context.require_gateway()
        resolved = await gateway.resolver.describe(require(arguments, "peer", self.name))
        chat_id = _marked_chat_id(resolved.id, resolved.kind)

        instruction = str(require(arguments, "instruction", self.name)).strip()[:4000]
        senders = await _resolve_senders(arguments.get("senders"), gateway)

        active = await context.watches.list_all(enabled_only=True)
        replacing = next((w for w in active if w.chat_id == chat_id), None)
        if replacing is None and len(active) >= settings.max_watches:
            raise ToolInputError(
                f"{len(active)} chats are already being answered automatically, which is "
                f"the configured limit ({settings.max_watches}). Stop one first with "
                f"autoreply_stop, or ask the owner which to drop."
            )

        now = datetime.now(UTC)
        max_replies = min(
            int(arguments.get("max_replies") or settings.max_replies_per_watch),
            settings.max_replies_per_watch,
        )
        watch = ChatWatch(
            chat_id=chat_id,
            chat_title=resolved.display,
            instruction=instruction,
            senders=senders,
            expires_at=ttl_for(settings, arguments.get("duration_minutes"), now=now),
            max_replies=max(1, max_replies),
            metadata={
                "created_by": "agent",
                "created_in_conversation": context.conversation_id,
                # Replies continue one thread of their own, distinct from the
                # conversation the operator set this up in and fresh for each
                # watch, so a new instruction does not inherit the old one's.
                "conversation_id": f"tg-autoreply-{chat_id}-{now.strftime('%Y%m%d%H%M%S')}",
            },
        )
        await context.watches.create(watch)

        payload = describe_watch(watch, now=now)
        payload["replaced_a_previous_watch"] = replacing is not None
        return ToolResult(content=_json(payload))


class AutoReplyListTool:
    name = "autoreply_list"
    description = (
        "Which chats are being answered automatically: the standing instruction, how many replies "
        "each has sent, and when it stops."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {"include_finished": boolean_field("Also list watches that have stopped.", default=False)}
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.watches is None:
            return ToolResult.error(_DISABLED)

        include_finished = bool(arguments.get("include_finished"))
        watches = await context.watches.list_all(enabled_only=not include_finished)
        now = datetime.now(UTC)
        payload = [describe_watch(w, now=now) for w in watches]
        return ToolResult(
            content=_json(
                {
                    "watches": payload,
                    "count": len(payload),
                    "autoreply_enabled": context.settings.autoreply.enabled,
                }
            )
        )


class AutoReplyStopTool:
    name = "autoreply_stop"
    description = (
        "Stop answering a chat automatically. Pass 'all' to stop every one at once, which is what "
        "'I'm back' means."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {"peer": string_field("The chat to stop answering, or 'all' to stop every one of them.")},
        required=["peer"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.watches is None:
            return ToolResult.error(_DISABLED)

        peer = str(require(arguments, "peer", self.name)).strip()
        if peer.lower() in ("all", "*", "everyone", "every chat"):
            stopped = await context.watches.disable_all(reason=STOPPED_BY_OPERATOR)
            return ToolResult(content=_json({"stopped": stopped, "scope": "all"}))

        gateway = context.require_gateway()
        resolved = await gateway.resolver.describe(peer)
        chat_id = _marked_chat_id(resolved.id, resolved.kind)
        watch = await context.watches.for_chat(chat_id)
        if watch is None:
            raise ToolInputError(
                f"{resolved.display} is not being answered automatically. "
                f"Use autoreply_list to see which chats are."
            )

        watch.enabled = False
        watch.stopped_because = STOPPED_BY_OPERATOR
        await context.watches.update(watch)
        return ToolResult(
            content=_json(
                {"stopped": resolved.display, "chat_id": chat_id, "replies_sent": watch.reply_count}
            )
        )


async def _resolve_senders(raw: Any, gateway: Any) -> list[int]:
    """Turn ``["@alex", "77"]`` into the user ids an arriving message carries.

    Sender ids are never marked — a message's ``sender_id`` is a plain user id —
    so unlike the chat, these need no adjusting, only resolving.
    """
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for entry in raw[:20]:
        reference = str(entry).strip()
        if not reference:
            continue
        try:
            ids.append(int(reference))
            continue
        except ValueError:
            pass
        resolved = await gateway.resolver.describe(reference)
        ids.append(int(resolved.id))
    return ids


def build_autoreply_tools() -> list[Any]:
    return [AutoReplyListTool(), AutoReplyStartTool(), AutoReplyStopTool()]
