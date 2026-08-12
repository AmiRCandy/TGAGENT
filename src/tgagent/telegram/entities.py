"""Peer resolution and argument coercion.

Two jobs that both sit between "what the model wrote" and "what Telethon needs":

**Resolution.** The model refers to peers the way a person does — ``@alex``,
``"Project X"``, ``-1001234567890``. Telethon needs an ``InputPeer``. Resolution
costs a network round trip, so results are cached for the process lifetime.

**Coercion.** Raw TL requests take typed arguments: ``InputPeer`` objects,
``datetime`` instances, filter classes. JSON only has strings, numbers, and
dicts. The coercion here reads the request class's own signature to decide what
each argument should become, which means it stays correct as the schema evolves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tgagent.errors import EntityResolutionError
from tgagent.observability.logging import get_logger

log = get_logger(__name__)

_PEER_ANNOTATIONS = ("InputPeer", "InputUser", "InputChannel", "InputChat", "InputDialogPeer")
_INT_PEER = re.compile(r"^-?\d+$")


@dataclass(slots=True, frozen=True)
class ResolvedPeer:
    """A peer, in every form the rest of the system needs."""

    #: What the caller asked for.
    reference: str
    #: Canonical numeric id.
    id: int
    #: ``user`` | ``chat`` | ``channel``.
    kind: str
    title: str = ""
    username: str | None = None

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.title or str(self.id)


class EntityResolver:
    """Resolves peer references, with a process-lifetime cache."""

    def __init__(self, client: Any, *, cache_size: int = 512) -> None:
        self._client = client
        self._cache: dict[str, Any] = {}
        self._info: dict[str, ResolvedPeer] = {}
        self._cache_size = cache_size

    @staticmethod
    def normalise(reference: Any) -> str:
        """Canonical cache key for a peer reference."""
        if isinstance(reference, str):
            return reference.strip().lstrip("@").lower()
        return str(reference)

    async def input_entity(self, reference: Any) -> Any:
        """Resolve to an ``InputPeer``, using the cache when possible."""
        key = self.normalise(reference)
        if cached := self._cache.get(key):
            return cached

        lookup: Any = reference
        # A bare numeric string is an id, not a username.
        if isinstance(reference, str) and _INT_PEER.match(reference.strip()):
            lookup = int(reference.strip())

        try:
            entity = await self._client.get_input_entity(lookup)
        except (ValueError, TypeError) as exc:
            raise EntityResolutionError(
                f"Could not resolve {reference!r} to a Telegram chat or user. "
                f"Try an @username, a numeric id, or list your dialogs first "
                f"so the reference is cached. ({exc})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - Telethon raises RPCErrors here
            raise EntityResolutionError(f"Resolving {reference!r} failed: {exc}") from exc

        self._remember(key, entity)
        return entity

    async def describe(self, reference: Any) -> ResolvedPeer:
        """Resolve and return human-meaningful details, for prompts and audits."""
        key = self.normalise(reference)
        if info := self._info.get(key):
            return info

        try:
            entity = await self._client.get_entity(reference)
        except Exception as exc:  # noqa: BLE001
            raise EntityResolutionError(f"Resolving {reference!r} failed: {exc}") from exc

        kind = "user"
        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            kind = "channel"
        elif type(entity).__name__ in ("Chat", "ChatForbidden"):
            kind = "chat"

        title = getattr(entity, "title", None) or " ".join(
            part
            for part in (getattr(entity, "first_name", ""), getattr(entity, "last_name", ""))
            if part
        )
        info = ResolvedPeer(
            reference=str(reference),
            id=int(getattr(entity, "id", 0)),
            kind=kind,
            title=title.strip(),
            username=getattr(entity, "username", None),
        )
        self._info[key] = info
        return info

    def _remember(self, key: str, entity: Any) -> None:
        if len(self._cache) >= self._cache_size:
            # Simple FIFO eviction; peer resolution has no meaningful recency
            # skew worth the complexity of an LRU here.
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = entity

    def clear(self) -> None:
        self._cache.clear()
        self._info.clear()


#: Argument names that denote a peer, used when the annotation is unavailable.
PEER_ARGUMENT_NAMES = frozenset(
    {
        "peer", "entity", "chat", "channel", "user", "user_id", "chat_id", "channel_id",
        "from_peer", "to_peer", "dialog", "bot", "target", "from_id", "participant",
    }
)


def extract_target(arguments: dict[str, Any]) -> str | None:
    """Best-effort identification of the chat an operation acts on.

    Used by the permission engine for allow/deny lists and by the audit trail.
    Returns ``None`` when the operation has no single obvious target.
    """
    for name in ("peer", "entity", "chat", "channel", "to_peer", "dialog", "target", "user"):
        if (value := arguments.get(name)) is not None:
            return _stringify_peer(value)
    for name, value in arguments.items():
        if name in PEER_ARGUMENT_NAMES and value is not None:
            return _stringify_peer(value)
    return None


def _stringify_peer(value: Any) -> str:
    if isinstance(value, (str, int)):
        return str(value)
    for attr in ("user_id", "channel_id", "chat_id", "id"):
        if (found := getattr(value, attr, None)) is not None:
            return str(found)
    if isinstance(value, dict):
        for attr in ("user_id", "channel_id", "chat_id", "id"):
            if (found := value.get(attr)) is not None:
                return str(found)
    return str(value)[:100]


async def coerce_argument(
    value: Any, annotation: str, resolver: EntityResolver, *, depth: int = 0
) -> Any:
    """Convert a JSON value into what a TL request constructor expects."""
    if value is None or depth > 6:
        return value

    # A dict tagged with "_" names a TL type to construct, e.g.
    # {"_": "InputMessagesFilterPhotos"}.
    if isinstance(value, dict) and "_" in value:
        return await _construct_tl_type(value, resolver, depth)

    if isinstance(value, list):
        return [await coerce_argument(item, annotation, resolver, depth=depth + 1) for item in value]

    if any(marker in annotation for marker in _PEER_ANNOTATIONS) and isinstance(value, (str, int)):
        return await resolver.input_entity(value)

    if "datetime" in annotation and isinstance(value, str):
        return parse_datetime(value)

    if "datetime" in annotation and isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)

    # A filter given as a bare class name, e.g. "InputMessagesFilterDocument".
    if "Filter" in annotation and isinstance(value, str):
        return _lookup_tl_type(value)()

    return value


async def _construct_tl_type(value: dict[str, Any], resolver: EntityResolver, depth: int) -> Any:
    import inspect

    type_name = value["_"]
    klass = _lookup_tl_type(type_name)
    signature = inspect.signature(klass.__init__)

    kwargs: dict[str, Any] = {}
    for key, item in value.items():
        if key == "_":
            continue
        parameter = signature.parameters.get(key)
        if parameter is None:
            raise EntityResolutionError(
                f"{type_name} has no parameter {key!r}. "
                f"Valid parameters: {', '.join(p for p in signature.parameters if p != 'self')}."
            )
        annotation = _render_annotation(parameter.annotation)
        kwargs[key] = await coerce_argument(item, annotation, resolver, depth=depth + 1)
    return klass(**kwargs)


def _lookup_tl_type(name: str) -> Any:
    """Find a class in ``telethon.tl.types``, including its sub-namespaces."""
    from telethon.tl import types

    bare = name.split(".")[-1]
    if "." in name:
        namespace = name.rsplit(".", 1)[0]
        module = getattr(types, namespace, None)
        if module is not None and (found := getattr(module, bare, None)) is not None:
            return found

    if (found := getattr(types, bare, None)) is not None:
        return found

    for attribute in dir(types):
        submodule = getattr(types, attribute, None)
        if submodule is not None and hasattr(submodule, bare) and not attribute.startswith("_"):
            candidate = getattr(submodule, bare)
            if isinstance(candidate, type):
                return candidate

    raise EntityResolutionError(
        f"Unknown Telegram type {name!r}. Use telegram_api_search to find the correct name."
    )


def _render_annotation(annotation: Any) -> str:
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", str(annotation))


def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 string, tolerating a trailing ``Z`` and bare dates."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EntityResolutionError(
            f"{value!r} is not a valid ISO-8601 date/time (e.g. 2026-01-31 or "
            f"2026-01-31T12:00:00Z)."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
