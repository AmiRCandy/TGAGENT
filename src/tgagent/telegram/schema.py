"""A searchable, offline index of the Telegram API surface.

This is how the agent discovers methods it does not already know without any of
the API reference sitting in the system prompt.

The index is built by **reflecting over the installed Telethon package** —
walking ``telethon.tl.functions.*`` for the ~824 generated TL request classes and
introspecting :class:`telethon.TelegramClient` for its friendly methods. Two
consequences follow, and both are the reason this approach was chosen over
shipping a static document:

* It can never drift from the version actually installed. Upgrade Telethon and
  the index describes the new surface on next build.
* It costs nothing at runtime beyond a JSON file on disk, and it works with no
  network access at all.

The result is cached under ``<data_dir>/cache`` and invalidated by Telethon's
version string.
"""

from __future__ import annotations

import inspect
import json
import pkgutil
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tgagent.observability.logging import get_logger

log = get_logger(__name__)

_RETURNS = re.compile(r":returns\s+([\w.]+):\s*(.*)")
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class ApiEntry:
    """One callable in the Telegram API surface."""

    #: ``messages.SendMessage`` for TL requests, ``send_message`` for friendly ones.
    path: str
    #: ``tl_request`` or ``client_method``.
    kind: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    returns: str = ""
    summary: str = ""
    #: Importable location, e.g. ``telethon.tl.functions.messages``.
    module: str = ""

    def signature(self) -> str:
        rendered = ", ".join(
            f"{p['name']}: {p['type']}" + ("" if p["required"] else " = ...")
            for p in self.parameters
        )
        return f"{self.path}({rendered})"

    def to_search_text(self) -> str:
        params = " ".join(p["name"] for p in self.parameters)
        return f"{self.path} {params} {self.returns} {self.summary}".lower()


@dataclass(slots=True)
class SearchHit:
    entry: ApiEntry
    score: float


class TelegramSchemaIndex:
    """Builds, caches, and searches the API index."""

    CACHE_VERSION = 2

    def __init__(self, cache_path: Path | None = None) -> None:
        self._cache_path = cache_path
        self._entries: list[ApiEntry] = []
        self._by_path: dict[str, ApiEntry] = {}
        self._search_text: list[str] = []
        self._loaded = False

    # --------------------------------------------------------------- build ---
    def ensure_loaded(self) -> None:
        """Load from cache, or build and cache. Idempotent."""
        if self._loaded:
            return
        if self._load_cache():
            self._loaded = True
            return

        log.info("schema.building")
        entries = build_index()
        self._install(entries)
        self._save_cache()
        self._loaded = True
        log.info("schema.built", entries=len(entries))

    def _install(self, entries: list[ApiEntry]) -> None:
        self._entries = entries
        self._by_path = {e.path.lower(): e for e in entries}
        self._search_text = [e.to_search_text() for e in entries]

    # ---------------------------------------------------------------- query --
    def get(self, path: str) -> ApiEntry | None:
        """Exact lookup, tolerant of a trailing ``Request`` and of case."""
        self.ensure_loaded()
        key = path.strip().lower()
        if entry := self._by_path.get(key):
            return entry
        if key.endswith("request"):
            return self._by_path.get(key[: -len("request")])
        return self._by_path.get(f"{key}request")

    def search(self, query: str, *, limit: int = 10, kind: str | None = None) -> list[SearchHit]:
        """Rank entries against a free-text query.

        Scoring favours, in order: an exact path match, the query appearing in
        the path, then per-token matches weighted by where they matched. It is a
        deliberately simple scheme — the corpus is under a thousand short
        documents, so anything heavier would be unjustified machinery.
        """
        self.ensure_loaded()
        tokens = _TOKEN.findall(query.lower())
        if not tokens:
            return []

        needle = query.strip().lower()
        hits: list[SearchHit] = []

        for entry, text in zip(self._entries, self._search_text, strict=True):
            if kind and entry.kind != kind:
                continue
            path = entry.path.lower()
            score = 0.0

            if path == needle or path.endswith(f".{needle}"):
                score += 100.0
            elif needle and needle in path:
                score += 40.0

            for token in tokens:
                if token in path:
                    score += 12.0
                    if path.split(".")[-1].startswith(token):
                        score += 6.0
                elif token in text:
                    score += 3.0

            if score > 0:
                # Prefer the friendly layer: it is easier to call correctly and
                # is what a first-time reader should reach for.
                if entry.kind == "client_method":
                    score *= 1.15
                hits.append(SearchHit(entry=entry, score=round(score, 2)))

        hits.sort(key=lambda h: (-h.score, len(h.entry.path)))
        return hits[:limit]

    def namespaces(self) -> list[str]:
        self.ensure_loaded()
        return sorted({e.path.split(".")[0] for e in self._entries if "." in e.path})

    def __len__(self) -> int:
        self.ensure_loaded()
        return len(self._entries)

    # --------------------------------------------------------------- cache ---
    def _load_cache(self) -> bool:
        if self._cache_path is None or not self._cache_path.exists():
            return False
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("schema.cache_unreadable", error=str(exc))
            return False

        # Parseable is not the same as usable: a file holding a list, a number or
        # ``null`` is still just a cache miss, and rebuilding is always possible.
        if not isinstance(payload, dict):
            log.warning(
                "schema.cache_malformed",
                error=f"expected a JSON object, got {type(payload).__name__}",
            )
            return False

        if payload.get("cache_version") != self.CACHE_VERSION:
            return False
        if payload.get("telethon_version") != _telethon_version():
            log.info("schema.cache_stale", cached=payload.get("telethon_version"))
            return False

        try:
            entries = [ApiEntry(**item) for item in payload["entries"]]
        except (KeyError, TypeError) as exc:
            log.warning("schema.cache_malformed", error=str(exc))
            return False

        self._install(entries)
        log.debug("schema.cache_hit", entries=len(entries))
        return True

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        payload = {
            "cache_version": self.CACHE_VERSION,
            "telethon_version": _telethon_version(),
            "entries": [asdict(e) for e in self._entries],
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError as exc:
            log.warning("schema.cache_write_failed", error=str(exc))


# ------------------------------------------------------------ construction --
def build_index() -> list[ApiEntry]:
    """Reflect over the installed Telethon and produce the full index."""
    return [*_build_tl_requests(), *_build_client_methods()]


def _build_tl_requests() -> list[ApiEntry]:
    from telethon.tl import functions
    from telethon.tl.tlobject import TLRequest

    entries: list[ApiEntry] = []

    def harvest(module: Any, namespace: str) -> None:
        for name in dir(module):
            if not name.endswith("Request"):
                continue
            obj = getattr(module, name, None)
            if not (isinstance(obj, type) and issubclass(obj, TLRequest)):
                continue
            bare = name[: -len("Request")]
            path = f"{namespace}.{bare}" if namespace else bare
            # `obj` is a class here, so its __init__ carries the TL signature.
            initialiser: Any = getattr(obj, "__init__")  # noqa: B009
            entries.append(
                ApiEntry(
                    path=path,
                    kind="tl_request",
                    parameters=_parameters_of(initialiser),
                    returns=_returns_of(initialiser),
                    summary=_summary_of(initialiser),
                    module=module.__name__,
                )
            )

    harvest(functions, "")
    for info in pkgutil.iter_modules(functions.__path__):
        try:
            submodule = __import__(f"telethon.tl.functions.{info.name}", fromlist=["_"])
        except ImportError as exc:  # pragma: no cover
            log.warning("schema.submodule_failed", module=info.name, error=str(exc))
            continue
        harvest(submodule, info.name)

    return entries


def _build_client_methods() -> list[ApiEntry]:
    from telethon import TelegramClient

    #: Methods that manage the connection or credentials rather than doing work.
    #: They are excluded so the agent is never nudged toward calling them.
    excluded = {
        "connect",
        "disconnect",
        "start",
        "run_until_disconnected",
        "log_out",
        "sign_in",
        "sign_up",
        "send_code_request",
        "qr_login",
        "edit_2fa",
        "add_event_handler",
        "remove_event_handler",
        "list_event_handlers",
        "session",
        "loop",
        "disconnected",
        "flood_sleep_threshold",
        "parse_mode",
        "set_proxy",
        "catch_up",
    }

    entries: list[ApiEntry] = []
    for name in dir(TelegramClient):
        if name.startswith("_") or name in excluded:
            continue
        member = getattr(TelegramClient, name, None)
        if not callable(member):
            continue
        entries.append(
            ApiEntry(
                path=name,
                kind="client_method",
                parameters=_parameters_of(member, skip_self=True),
                returns="",
                summary=_summary_of(member),
                module="telethon.TelegramClient",
            )
        )
    return entries


def _parameters_of(func: Any, *, skip_self: bool = True) -> list[dict[str, Any]]:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - C-level callables
        return []

    out: list[dict[str, Any]] = []
    for name, parameter in signature.parameters.items():
        if skip_self and name in ("self", "cls"):
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = parameter.annotation
        rendered = (
            _clean_annotation(annotation) if annotation is not inspect.Parameter.empty else "Any"
        )
        out.append(
            {
                "name": name,
                "type": rendered,
                "required": parameter.default is inspect.Parameter.empty,
            }
        )
    return out


def _clean_annotation(annotation: Any) -> str:
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", None)
    if text is None:
        text = str(annotation)
    text = text.replace("typing.", "").replace("telethon.", "")
    text = re.sub(r"ForwardRef\('([^']+)'\)", r"\1", text)
    return text.strip("'\" ")[:120]


def _returns_of(func: Any) -> str:
    doc = inspect.getdoc(func) or ""
    if match := _RETURNS.search(doc):
        return match.group(1)
    return ""


def _summary_of(func: Any) -> str:
    doc = inspect.getdoc(func) or ""
    lines = [line.strip() for line in doc.splitlines() if line.strip()]
    for line in lines:
        if line.startswith((":", "Arguments", "Example", ">>>", "Returns")):
            continue
        return line[:280]
    return ""


def _telethon_version() -> str:
    try:
        import telethon

        return str(telethon.__version__)
    except Exception:  # noqa: BLE001 - version is advisory for cache keying
        return "unknown"


def format_entry(entry: ApiEntry) -> str:
    """Human/model-readable rendering used by the ``telegram_api_search`` tool."""
    lines = [f"{entry.signature()}", f"  kind    : {entry.kind}"]
    if entry.returns:
        lines.append(f"  returns : {entry.returns}")
    if entry.summary:
        lines.append(f"  summary : {entry.summary}")
    if entry.kind == "tl_request":
        lines.append(f'  call as : tg.invoke_raw("{entry.path}", {{...}})')
    else:
        lines.append(f"  call as : tg.{entry.path}(...)")
    return "\n".join(lines)
