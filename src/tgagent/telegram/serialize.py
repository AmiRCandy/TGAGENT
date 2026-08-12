"""Turning Telethon objects into safe, compact JSON.

Three problems have to be solved at once, and they pull against each other:

**Safety.** TL objects contain ``bytes`` (file references, auth keys), circular
references (a message points at a chat that lists the message), and objects
whose ``repr`` includes credentials. None of that may reach the model or the
sandbox.

**Size.** A raw ``Message`` serialises to several hundred tokens of mostly-null
fields. A dialog list of 200 chats would blow the context window on its own. So
the well-known types get hand-written compact projections, and everything else
gets a generic walk with depth, breadth, and string-length caps.

**Fidelity.** The agent still has to be able to do real work, which means ids,
dates, reply/forward links, and media metadata must survive.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from enum import Enum
from typing import Any, Final

#: Longest string kept verbatim; longer values are truncated with a marker.
DEFAULT_MAX_STRING: Final = 4096
#: How deep the generic walker descends before summarising.
DEFAULT_MAX_DEPTH: Final = 6
#: Longest list kept before truncation.
DEFAULT_MAX_ITEMS: Final = 200
#: ``bytes`` shorter than this are base64-encoded; longer ones are described.
MAX_INLINE_BYTES: Final = 256

#: Attributes never emitted, whatever the object. These are either credentials
#: or huge blobs with no analytical value.
_FORBIDDEN_ATTRS: Final = frozenset(
    {
        "auth_key", "authkey", "key", "session", "_client", "client", "api_hash",
        "api_id", "dc_options", "file_reference", "bytes", "salt", "server_salt",
        "secret", "password", "srp_id", "srp_B", "g_a", "g_b", "nonce",
        "_sender", "_input_sender", "_chat", "_input_chat", "_forward",
        "_action_entities", "_client_ref",
    }
)


def truncate(text: str, limit: int = DEFAULT_MAX_STRING) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated, {len(text)} chars total]"


def to_jsonable(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_string: int = DEFAULT_MAX_STRING,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Any:
    """Recursively convert *value* into something ``json.dumps`` accepts."""
    return _convert(value, max_depth, max_string, max_items, set())


def _convert(
    value: Any, depth: int, max_string: int, max_items: int, seen: set[int]
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return truncate(value, max_string)

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) <= MAX_INLINE_BYTES:
            return {"__bytes__": base64.b64encode(raw).decode("ascii"), "length": len(raw)}
        return {"__bytes__": "<omitted>", "length": len(raw)}

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Enum):
        return value.value

    if depth <= 0:
        return f"<{type(value).__name__}: depth limit reached>"

    # Cycles are common in TL graphs, so identity tracking is mandatory rather
    # than defensive. Scalars are excluded above, so id() is safe to use here.
    marker = id(value)
    if marker in seen:
        return f"<{type(value).__name__}: circular reference>"

    if isinstance(value, dict):
        seen = seen | {marker}
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= max_items:
                out["__truncated__"] = f"{len(value) - max_items} more keys"
                break
            if _is_forbidden(key):
                continue
            out[str(key)] = _convert(item, depth - 1, max_string, max_items, seen)
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        seen = seen | {marker}
        items = list(value)
        converted = [
            _convert(item, depth - 1, max_string, max_items, seen) for item in items[:max_items]
        ]
        if len(items) > max_items:
            converted.append(f"… {len(items) - max_items} more items")
        return converted

    # A TL object: Telethon gives every one a to_dict(), but it recurses into the
    # whole graph, so the attribute walk below is used instead for control.
    if hasattr(value, "__dict__") or hasattr(value, "__slots__"):
        seen = seen | {marker}
        return _convert_object(value, depth, max_string, max_items, seen)

    return truncate(str(value), max_string)


def _convert_object(
    obj: Any, depth: int, max_string: int, max_items: int, seen: set[int]
) -> dict[str, Any]:
    out: dict[str, Any] = {"_": type(obj).__name__}
    names = _attribute_names(obj)
    for name in names:
        if _is_forbidden(name):
            continue
        try:
            item = getattr(obj, name)
        except Exception:  # noqa: BLE001 - properties can raise; skip them
            continue
        if item is None or callable(item):
            continue
        out[name] = _convert(item, depth - 1, max_string, max_items, seen)
    return out


def _attribute_names(obj: Any) -> list[str]:
    names: list[str] = []
    if hasattr(obj, "__slots__"):
        for klass in type(obj).__mro__:
            names.extend(getattr(klass, "__slots__", ()) or ())
    names.extend(getattr(obj, "__dict__", {}).keys())
    # Preserve order, drop duplicates and privates.
    seen: set[str] = set()
    return [n for n in names if not n.startswith("_") and not (n in seen or seen.add(n))]


def _is_forbidden(name: Any) -> bool:
    return isinstance(name, str) and (
        name in _FORBIDDEN_ATTRS or name.lower() in _FORBIDDEN_ATTRS
    )


# ------------------------------------------------------ compact projections --
def message_to_dict(message: Any, *, max_text: int = 4000) -> dict[str, Any]:
    """A compact, analysis-ready projection of a Telethon ``Message``.

    Roughly a tenth the size of the raw object while keeping everything the
    agent actually reasons over: who, when, what, what it replies to, whether it
    carries media, and how it was reacted to.
    """
    out: dict[str, Any] = {
        "id": getattr(message, "id", None),
        "date": _iso(getattr(message, "date", None)),
        "text": truncate(getattr(message, "message", None) or "", max_text),
        "out": bool(getattr(message, "out", False)),
    }

    if (sender_id := _peer_id(getattr(message, "from_id", None))) is not None:
        out["sender_id"] = sender_id
    elif (sender := getattr(message, "sender_id", None)) is not None:
        out["sender_id"] = sender

    if (chat_id := getattr(message, "chat_id", None)) is not None:
        out["chat_id"] = chat_id

    if reply := getattr(message, "reply_to", None):
        reply_id = getattr(reply, "reply_to_msg_id", None)
        if reply_id is not None:
            out["reply_to_msg_id"] = reply_id
        if (top := getattr(reply, "reply_to_top_id", None)) is not None:
            out["thread_id"] = top

    if fwd := getattr(message, "fwd_from", None):
        out["forwarded_from"] = {
            "date": _iso(getattr(fwd, "date", None)),
            "from_id": _peer_id(getattr(fwd, "from_id", None)),
            "from_name": getattr(fwd, "from_name", None),
        }

    if media := getattr(message, "media", None):
        out["media"] = _media_summary(media)

    if (edit_date := getattr(message, "edit_date", None)) is not None:
        out["edited_at"] = _iso(edit_date)
    if getattr(message, "pinned", False):
        out["pinned"] = True
    if (views := getattr(message, "views", None)) is not None:
        out["views"] = views

    if reactions := getattr(message, "reactions", None):
        results = getattr(reactions, "results", None) or []
        summary = []
        for r in results[:12]:
            emoticon = getattr(getattr(r, "reaction", None), "emoticon", None)
            summary.append({"reaction": emoticon, "count": getattr(r, "count", 0)})
        if summary:
            out["reactions"] = summary

    if action := getattr(message, "action", None):
        out["service_action"] = type(action).__name__

    return out


def dialog_to_dict(dialog: Any) -> dict[str, Any]:
    """Compact projection of a Telethon ``Dialog``."""
    entity = getattr(dialog, "entity", None)
    out: dict[str, Any] = {
        "id": getattr(dialog, "id", None),
        "name": truncate(getattr(dialog, "name", None) or "", 256),
        "unread_count": getattr(dialog, "unread_count", 0),
        "is_user": bool(getattr(dialog, "is_user", False)),
        "is_group": bool(getattr(dialog, "is_group", False)),
        "is_channel": bool(getattr(dialog, "is_channel", False)),
        "pinned": bool(getattr(dialog, "pinned", False)),
        "archived": bool(getattr(dialog, "archived", False)),
    }
    if (mentions := getattr(dialog, "unread_mentions_count", 0)):
        out["unread_mentions"] = mentions
    if entity is not None:
        if username := getattr(entity, "username", None):
            out["username"] = username
        if getattr(entity, "bot", False):
            out["is_bot"] = True
        if getattr(entity, "verified", False):
            out["verified"] = True
    if message := getattr(dialog, "message", None):
        out["last_message"] = {
            "id": getattr(message, "id", None),
            "date": _iso(getattr(message, "date", None)),
            "text": truncate(getattr(message, "message", None) or "", 200),
            "out": bool(getattr(message, "out", False)),
        }
    return out


def entity_to_dict(entity: Any) -> dict[str, Any]:
    """Compact projection of a User / Chat / Channel."""
    kind = type(entity).__name__
    out: dict[str, Any] = {"_": kind, "id": getattr(entity, "id", None)}
    for attr in (
        "username", "first_name", "last_name", "title", "phone", "bot", "verified",
        "scam", "fake", "premium", "deleted", "megagroup", "broadcast", "restricted",
        "participants_count", "about",
    ):
        value = getattr(entity, attr, None)
        if value not in (None, False):
            out[attr] = truncate(value, 500) if isinstance(value, str) else value
    # A phone number is personal data; keep only enough to recognise it.
    if phone := out.get("phone"):
        out["phone"] = f"…{str(phone)[-4:]}"
    return out


def participant_to_dict(user: Any) -> dict[str, Any]:
    out = entity_to_dict(user)
    if participant := getattr(user, "participant", None):
        out["role"] = type(participant).__name__
    return out


def _media_summary(media: Any) -> dict[str, Any]:
    """Metadata only — never the file contents."""
    kind = type(media).__name__
    out: dict[str, Any] = {"type": kind}

    document = getattr(media, "document", None)
    if document is not None:
        out["mime_type"] = getattr(document, "mime_type", None)
        out["size"] = getattr(document, "size", None)
        out["document_id"] = getattr(document, "id", None)
        for attribute in getattr(document, "attributes", None) or []:
            if filename := getattr(attribute, "file_name", None):
                out["file_name"] = truncate(filename, 256)
            if (duration := getattr(attribute, "duration", None)) is not None:
                out["duration"] = duration
            if getattr(attribute, "voice", False):
                out["voice"] = True
            if getattr(attribute, "round_message", False):
                out["video_note"] = True
            width, height = getattr(attribute, "w", None), getattr(attribute, "h", None)
            if width and height:
                out["dimensions"] = [width, height]

    photo = getattr(media, "photo", None)
    if photo is not None:
        out["photo_id"] = getattr(photo, "id", None)
        sizes = getattr(photo, "sizes", None) or []
        if sizes:
            largest = max(
                (s for s in sizes if getattr(s, "w", None)),
                key=lambda s: getattr(s, "w", 0),
                default=None,
            )
            if largest is not None:
                out["dimensions"] = [getattr(largest, "w", 0), getattr(largest, "h", 0)]

    if webpage := getattr(media, "webpage", None):
        out["webpage"] = {
            "url": truncate(getattr(webpage, "url", "") or "", 500),
            "title": truncate(getattr(webpage, "title", "") or "", 300),
        }
    if (poll := getattr(media, "poll", None)) is not None:
        question = getattr(poll, "question", None)
        out["poll"] = truncate(getattr(question, "text", None) or str(question or ""), 300)
    if (contact := getattr(media, "phone_number", None)) is not None:
        out["contact"] = f"…{str(contact)[-4:]}"
    if (geo := getattr(media, "geo", None)) is not None:
        out["geo"] = {"lat": getattr(geo, "lat", None), "long": getattr(geo, "long", None)}

    return {k: v for k, v in out.items() if v is not None}


def extract_text_fields(payload: Any) -> list[str]:
    """Collect every free-text string in a payload.

    Used to feed the injection scanner: it should see message bodies, captions,
    filenames, chat titles and usernames — every place an attacker can write.
    """
    found: list[str] = []
    _collect_text(payload, found, 0)
    return found


_TEXT_KEYS: Final = frozenset(
    {
        "text", "message", "caption", "title", "name", "first_name", "last_name",
        "about", "username", "file_name", "url", "from_name", "description", "bio",
        "service_action",
    }
)


def _collect_text(node: Any, out: list[str], depth: int) -> None:
    if depth > DEFAULT_MAX_DEPTH or len(out) > 5000:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key in _TEXT_KEYS and value:
                out.append(value)
            else:
                _collect_text(value, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_text(item, out, depth + 1)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else None


def _peer_id(peer: Any) -> int | None:
    """Extract the numeric id from a ``PeerUser``/``PeerChat``/``PeerChannel``."""
    if peer is None:
        return None
    for attr in ("user_id", "chat_id", "channel_id"):
        if (value := getattr(peer, attr, None)) is not None:
            return int(value)
    return None
