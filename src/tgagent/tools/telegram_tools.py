"""Curated Telegram tools.

The ~90% path. These wrap the operations that dominate real usage in
hand-written schemas with pre-shrunk output, so the common case is cheap,
reliable, and legible in an audit log. Anything not covered here is still
reachable through ``telegram_invoke`` (one raw call) or ``python`` (arbitrary
composition) — see ``docs/tool-architecture.md``.

Every tool here routes through :class:`~tgagent.telegram.gateway.TelegramGateway`,
so the permission engine sees them exactly as it sees generated code.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from tgagent.errors import ToolInputError
from tgagent.risk import RiskTier
from tgagent.tools.base import (
    ToolContext,
    ToolResult,
    boolean_field,
    clamp_int,
    integer_field,
    object_schema,
    require,
    string_field,
)


#: Compact JSON: the model reads it fine and it costs far fewer tokens than
#: indented output.
def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


_PEER_FIELD = string_field(
    "Chat or user: an @username, a numeric id (e.g. -1001234567890), a phone "
    "number, or 'me' for Saved Messages."
)


class _TelegramTool:
    """Shared plumbing for the curated tools."""

    name = ""
    description = ""
    parameters: ClassVar[dict[str, Any]] = object_schema({})
    risk_hint = RiskTier.READ_ONLY

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raise NotImplementedError


# ------------------------------------------------------------------ reads ---
class ListDialogsTool(_TelegramTool):
    name = "telegram_list_dialogs"
    description = (
        "List the account's conversations (private chats, groups, channels), most "
        "recently active first. Use this to discover what chats exist and which have "
        "unread messages, before reading or searching any of them."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {
            "limit": integer_field(
                "How many dialogs to return (1-200).", default=50, minimum=1, maximum=200
            ),
            "only_unread": boolean_field("Return only chats with unread messages.", default=False),
            "archived": boolean_field(
                "List archived chats instead of the main list.", default=False
            ),
        }
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        history = context.require_history()
        dialogs = await history.list_dialogs(
            limit=clamp_int(arguments.get("limit"), default=50, minimum=1, maximum=200),
            archived=bool(arguments.get("archived", False)),
            only_unread=bool(arguments.get("only_unread", False)),
            context=context.call_context(),
        )
        return ToolResult.untrusted(
            _json({"dialogs": dialogs, "count": len(dialogs)}),
            source="telegram:dialogs",
            count=len(dialogs),
        )


class ResolvePeerTool(_TelegramTool):
    name = "telegram_resolve_peer"
    description = (
        "Resolve a chat or user reference to its canonical id, type, and display name. "
        "Use this to turn a name like '@alex' or 'Project X' into an id you can pass to "
        "other tools, and to confirm you have the right person before sending anything."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema({"peer": _PEER_FIELD}, required=["peer"])

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        resolved = await gateway.resolver.describe(peer)
        return ToolResult.untrusted(
            _json(
                {
                    "id": resolved.id,
                    "kind": resolved.kind,
                    "title": resolved.title,
                    "username": resolved.username,
                    "display": resolved.display,
                }
            ),
            source=f"telegram:peer/{resolved.id}",
        )


class ReadHistoryTool(_TelegramTool):
    name = "telegram_read_history"
    description = (
        "Read a page of messages from one chat, newest first by default. Supports "
        "cursor pagination: pass the returned 'next_offset_id' back as 'offset_id' to "
        "continue. Prefer telegram_search_messages when you know what you are looking "
        "for — it filters server-side and is far cheaper than paging through everything. "
        "For scanning thousands of messages, use the `python` tool instead."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "limit": integer_field(
                "Messages per page (1-200).", default=50, minimum=1, maximum=200
            ),
            "offset_id": integer_field(
                "Continue from this message id (use 'next_offset_id' from a previous call).",
                default=0,
            ),
            "offset_date": string_field(
                "Only messages older than this ISO-8601 date/time, e.g. '2026-02-01'."
            ),
            "min_id": integer_field("Only messages with an id greater than this.", default=0),
            "reverse": boolean_field(
                "Read oldest-first instead of newest-first. Use for chronological summaries.",
                default=False,
            ),
        },
        required=["peer"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        history = context.require_history()
        peer = require(arguments, "peer", self.name)
        page = await history.read(
            peer,
            limit=clamp_int(arguments.get("limit"), default=50, minimum=1, maximum=200),
            offset_id=clamp_int(arguments.get("offset_id"), default=0, minimum=0, maximum=2**31),
            offset_date=arguments.get("offset_date"),
            min_id=clamp_int(arguments.get("min_id"), default=0, minimum=0, maximum=2**31),
            reverse=bool(arguments.get("reverse", False)),
            context=context.call_context(),
        )
        return ToolResult.untrusted(
            _json(page.to_dict()), source=f"telegram:chat/{peer}", count=len(page.messages)
        )


class SearchMessagesTool(_TelegramTool):
    name = "telegram_search_messages"
    description = (
        "Search message text. With 'peer', searches inside that chat; without it, "
        "searches every chat the account can see. Filtering happens on Telegram's "
        "servers, so this is dramatically cheaper than reading history and scanning it. "
        "Supports date bounds and sender/media filters."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {
            "query": string_field("Text to search for."),
            "peer": string_field("Restrict to one chat. Omit to search globally across all chats."),
            "limit": integer_field("Results per page (1-200).", default=50),
            "offset_id": integer_field("Continue from this message id.", default=0),
            "min_date": string_field("Only messages on or after this ISO-8601 date."),
            "max_date": string_field("Only messages on or before this ISO-8601 date."),
            "from_user": string_field("Only messages sent by this user (chat search only)."),
            "media_filter": string_field(
                "Restrict to a media kind. One of: photo, video, document, audio, voice, "
                "url, gif, music, chat_photo, round_video, sticker.",
                enum=[
                    "photo",
                    "video",
                    "document",
                    "audio",
                    "voice",
                    "url",
                    "gif",
                    "music",
                    "chat_photo",
                    "round_video",
                    "sticker",
                ],
            ),
        },
        required=["query"],
    )

    #: Friendly names → the Telethon filter classes.
    _FILTERS: ClassVar[dict[str, str]] = {
        "photo": "InputMessagesFilterPhotos",
        "video": "InputMessagesFilterVideo",
        "document": "InputMessagesFilterDocument",
        "audio": "InputMessagesFilterMusic",
        "music": "InputMessagesFilterMusic",
        "voice": "InputMessagesFilterVoice",
        "url": "InputMessagesFilterUrl",
        "gif": "InputMessagesFilterGif",
        "chat_photo": "InputMessagesFilterChatPhotos",
        "round_video": "InputMessagesFilterRoundVideo",
        "sticker": "InputMessagesFilterDocument",
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        history = context.require_history()
        query = require(arguments, "query", self.name)
        limit = clamp_int(arguments.get("limit"), default=50, minimum=1, maximum=200)
        offset_id = clamp_int(arguments.get("offset_id"), default=0, minimum=0, maximum=2**31)

        media_filter = None
        if raw_filter := arguments.get("media_filter"):
            media_filter = self._FILTERS.get(str(raw_filter).lower())
            if media_filter is None:
                raise ToolInputError(
                    f"Unknown media_filter {raw_filter!r}. "
                    f"Valid values: {', '.join(sorted(self._FILTERS))}."
                )

        if peer := arguments.get("peer"):
            page = await history.search_in_chat(
                peer,
                query,
                limit=limit,
                offset_id=offset_id,
                min_date=arguments.get("min_date"),
                max_date=arguments.get("max_date"),
                from_user=arguments.get("from_user"),
                media_filter=media_filter,
                context=context.call_context(),
            )
            source = f"telegram:chat/{peer}"
        else:
            page = await history.search_global(
                query,
                limit=limit,
                offset_id=offset_id,
                min_date=arguments.get("min_date"),
                max_date=arguments.get("max_date"),
                context=context.call_context(),
            )
            source = "telegram:global-search"

        return ToolResult.untrusted(_json(page.to_dict()), source=source, count=len(page.messages))


class GetParticipantsTool(_TelegramTool):
    name = "telegram_get_participants"
    description = (
        "List the members of a group or channel, with their roles where visible. "
        "Large channels only expose a subset of members."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "limit": integer_field("Maximum members to return (1-200).", default=100),
            "search": string_field("Filter members by name or username."),
        },
        required=["peer"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from tgagent.telegram.serialize import participant_to_dict

        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        call_arguments: dict[str, Any] = {
            "entity": peer,
            "limit": clamp_int(arguments.get("limit"), default=100, minimum=1, maximum=200),
        }
        if search := arguments.get("search"):
            call_arguments["search"] = search

        result = await gateway.call(
            "get_participants",
            call_arguments,
            context=context.call_context(),
            projector=lambda users: [participant_to_dict(u) for u in users],
        )
        rows = result.payload if isinstance(result.payload, list) else []
        return ToolResult.untrusted(
            _json({"participants": rows, "count": len(rows)}),
            source=f"telegram:chat/{peer}/participants",
        )


class GetMeTool(_TelegramTool):
    name = "telegram_get_me"
    description = (
        "Return the signed-in account's own id, username, and display name. Use this "
        "to tell the user's own messages apart from other people's, and to resolve "
        "first-person references like 'messages I sent'."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema({})

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from tgagent.telegram.serialize import entity_to_dict

        gateway = context.require_gateway()
        result = await gateway.call(
            "get_me", {}, context=context.call_context(), projector=entity_to_dict
        )
        # The account's own profile is operator data, not third-party content.
        return ToolResult(content=_json(result.payload), source="telegram:me")


# ----------------------------------------------------------------- writes ---
class SendMessageTool(_TelegramTool):
    name = "telegram_send_message"
    description = (
        "Send a text message to a chat or user. This is visible to other people and "
        "cannot be un-sent for them once delivered, so it requires confirmation under "
        "the default policy. Resolve the peer first if there is any ambiguity about "
        "who you are messaging."
    )
    risk_hint = RiskTier.EXTERNALLY_VISIBLE
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "message": string_field("The message text. Markdown is supported."),
            "reply_to": integer_field("Reply to this message id."),
            "silent": boolean_field("Send without triggering a notification.", default=False),
        },
        required=["peer", "message"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from tgagent.telegram.serialize import message_to_dict

        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        text = require(arguments, "message", self.name)

        call_arguments: dict[str, Any] = {"entity": peer, "message": text}
        if reply_to := arguments.get("reply_to"):
            call_arguments["reply_to"] = int(reply_to)
        if arguments.get("silent"):
            call_arguments["silent"] = True

        result = await gateway.call(
            "send_message",
            call_arguments,
            context=context.call_context(),
            projector=message_to_dict,
        )
        sent = result.payload if isinstance(result.payload, dict) else {}
        return ToolResult(
            content=_json({"sent": True, "message_id": sent.get("id"), "peer": str(peer)}),
            metadata={"peer": str(peer), "message_id": sent.get("id")},
        )


class EditMessageTool(_TelegramTool):
    name = "telegram_edit_message"
    description = (
        "Edit a message the account previously sent. Other participants see an "
        "'edited' marker. Only the account's own messages can be edited."
    )
    risk_hint = RiskTier.EXTERNALLY_VISIBLE
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "message_id": integer_field("The id of the message to edit."),
            "text": string_field("The replacement text."),
        },
        required=["peer", "message_id", "text"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        await gateway.call(
            "edit_message",
            {
                "entity": peer,
                "message": int(require(arguments, "message_id", self.name)),
                "text": require(arguments, "text", self.name),
            },
            context=context.call_context(),
            projector=lambda _msg: {"edited": True},
        )
        return ToolResult(content=_json({"edited": True, "peer": str(peer)}))


class ForwardMessagesTool(_TelegramTool):
    name = "telegram_forward_messages"
    description = (
        "Forward one or more messages from one chat to another. The recipient sees the "
        "original author. Externally visible, so it requires confirmation by default."
    )
    risk_hint = RiskTier.EXTERNALLY_VISIBLE
    parameters = object_schema(
        {
            "from_peer": string_field("The chat the messages are currently in."),
            "to_peer": string_field("The chat to forward them to."),
            "message_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Ids of the messages to forward (max 100).",
                "maxItems": 100,
            },
        },
        required=["from_peer", "to_peer", "message_ids"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        ids = require(arguments, "message_ids", self.name)
        if not isinstance(ids, list) or not ids:
            raise ToolInputError("message_ids must be a non-empty array of integers.")
        if len(ids) > 100:
            raise ToolInputError("Telegram forwards at most 100 messages per call.")

        to_peer = require(arguments, "to_peer", self.name)
        await gateway.call(
            "forward_messages",
            {
                "entity": to_peer,
                "messages": [int(i) for i in ids],
                "from_peer": require(arguments, "from_peer", self.name),
            },
            context=context.call_context(),
            projector=lambda msgs: {"forwarded": len(msgs) if isinstance(msgs, list) else 1},
        )
        return ToolResult(content=_json({"forwarded": len(ids), "to": str(to_peer)}))


class DeleteMessagesTool(_TelegramTool):
    name = "telegram_delete_messages"
    description = (
        "Delete messages. THIS IS NOT REVERSIBLE. With revoke=true the messages are "
        "removed for everyone, not just this account. Requires confirmation under the "
        "default policy; confirm the exact ids with the user before calling."
    )
    risk_hint = RiskTier.DESTRUCTIVE
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "message_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Ids of the messages to delete (max 100).",
                "maxItems": 100,
            },
            "revoke": boolean_field("Delete for everyone, not just this account.", default=True),
        },
        required=["peer", "message_ids"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        ids = require(arguments, "message_ids", self.name)
        if not isinstance(ids, list) or not ids:
            raise ToolInputError("message_ids must be a non-empty array of integers.")

        peer = require(arguments, "peer", self.name)
        await gateway.call(
            "delete_messages",
            {
                "entity": peer,
                "message_ids": [int(i) for i in ids],
                "revoke": bool(arguments.get("revoke", True)),
            },
            context=context.call_context(),
            projector=lambda _r: {"deleted": True},
        )
        return ToolResult(content=_json({"deleted": len(ids), "peer": str(peer)}))


class MarkReadTool(_TelegramTool):
    name = "telegram_mark_read"
    description = (
        "Mark a chat as read. Reversible in the sense that it changes only this "
        "account's read state, though the sender may see read receipts."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema({"peer": _PEER_FIELD}, required=["peer"])

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        peer = require(arguments, "peer", self.name)
        await gateway.call(
            "send_read_acknowledge",
            {"entity": peer},
            context=context.call_context(),
            projector=lambda _r: {"ok": True},
        )
        return ToolResult(content=_json({"marked_read": str(peer)}))


class DownloadMediaTool(_TelegramTool):
    name = "telegram_download_media"
    description = (
        "Download the file attached to a message to local disk, returning its path and "
        "metadata. Size and MIME type are validated before the transfer starts, and "
        "executable types are refused. Downloaded files are never executed or opened."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {
            "peer": _PEER_FIELD,
            "message_id": integer_field("Id of the message whose media to download."),
            "file_name": string_field("Override the filename (it will be sanitised)."),
        },
        required=["peer", "message_id"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.media is None or not context.settings.features.media_download:
            return ToolResult.error("Media download is disabled in this deployment.")

        peer = require(arguments, "peer", self.name)
        message_id = int(require(arguments, "message_id", self.name))
        downloaded = await context.media.download_message_media(
            peer,
            message_id,
            run_id=context.run_id,
            context=context.call_context(),
            file_name_override=arguments.get("file_name"),
        )
        return ToolResult(content=_json(downloaded.to_dict()), metadata=downloaded.to_dict())


# ------------------------------------------------------------- raw access ---
class InvokeTool(_TelegramTool):
    name = "telegram_invoke"
    description = (
        "Call any Telegram API method directly — the full ~800-method surface, not "
        "just the curated tools. Use telegram_api_search first to find the method name "
        "and its parameters. Method names are either raw TL requests like "
        "'messages.Search' or 'channels.GetParticipants', or Telethon client methods "
        "like 'get_messages'. Peer arguments accept @usernames and ids and are resolved "
        "for you. For anything needing loops or filtering across many results, use the "
        "`python` tool instead — it is one call rather than many."
    )
    risk_hint = RiskTier.DESTRUCTIVE  # the classifier decides per-method
    parameters = object_schema(
        {
            "method": string_field(
                "The method, e.g. 'messages.Search', 'channels.GetFullChannel', or 'get_messages'."
            ),
            "params": {
                "type": "object",
                "description": "Arguments as a JSON object, matching the method signature.",
                "additionalProperties": True,
            },
        },
        required=["method"],
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        gateway = context.require_gateway()
        method = str(require(arguments, "method", self.name))
        params = arguments.get("params") or {}
        if not isinstance(params, dict):
            raise ToolInputError("`params` must be a JSON object.")

        result = await gateway.call(method, params, context=context.call_context())
        return ToolResult.untrusted(
            _json(result.payload), source=f"telegram:invoke/{method}", method=method
        )


def build_telegram_tools() -> list[Any]:
    """The curated tool set, in the order it is advertised to the model."""
    return [
        ListDialogsTool(),
        ResolvePeerTool(),
        ReadHistoryTool(),
        SearchMessagesTool(),
        GetParticipantsTool(),
        GetMeTool(),
        DownloadMediaTool(),
        MarkReadTool(),
        SendMessageTool(),
        EditMessageTool(),
        ForwardMessagesTool(),
        DeleteMessagesTool(),
        InvokeTool(),
    ]
