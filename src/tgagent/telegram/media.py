"""Media download with validation, quarantine, and cleanup.

Files arriving from Telegram are **hostile input**. The rules enforced here:

* **Size is checked before the transfer starts**, from the document metadata,
  not after a 2 GB file has landed on disk.
* **MIME type must be on an allow-list**, and the extension must not be on a
  blocklist. Both are checked, because either alone is trivially bypassed. Media
  declaring no MIME type is refused unless its kind implies one — a photo is
  always JPEG — so omitting the field is not a way past the allow-list.
* **Filenames are sanitised, never trusted.** A caption-supplied name like
  ``../../.ssh/authorized_keys`` is reduced to a leaf name, and the resolved
  path is verified to still sit inside the download directory.
* **Downloads land in a per-run directory** under the configured root, so a run
  can be cleaned up wholesale and one run cannot overwrite another's files.
* Nothing downloaded is ever executed, imported, or handed to the sandbox
  process. The sandbox learns a path and metadata, never contents.
"""

from __future__ import annotations

import contextlib
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tgagent.config.settings import MediaSettings
from tgagent.errors import MediaError, MediaTooLarge, MediaTypeRejected
from tgagent.observability.logging import get_logger
from tgagent.telegram.gateway import CallContext, TelegramGateway

log = get_logger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_LEADING_DOTS = re.compile(r"^\.+")

#: Media kinds Telegram serves with no MIME type of their own, and the type they
#: are in fact. Photos are always JPEG on the wire, so they can still be matched
#: against the allow-list rather than skipping it.
_IMPLICIT_MIME_TYPES = {"MessageMediaPhoto": "image/jpeg"}


@dataclass(slots=True)
class DownloadResult:
    path: Path
    size_bytes: int
    mime_type: str | None
    file_name: str
    message_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "message_id": self.message_id,
        }


def sanitise_filename(name: str | None, *, fallback: str = "file") -> str:
    """Reduce an arbitrary string to a safe leaf filename.

    Path separators, traversal sequences, control characters, and Windows
    reserved device names are all removed. The result is always a plain name
    with no directory component.

    The *fallback* is sanitised on exactly the same terms: callers build it from
    untrusted material such as a peer reference, so trusting it would reopen the
    traversal it is meant to avoid.
    """
    return _safe_leaf(name) or _safe_leaf(fallback) or "file"


def _safe_leaf(name: str | None) -> str:
    """Sanitise one candidate name, returning ``""`` when nothing usable is left."""
    if not name:
        return ""

    # Strip any directory component the sender tried to smuggle in.
    candidate = name.replace("\\", "/").split("/")[-1]
    candidate = unicodedata.normalize("NFKD", candidate)
    candidate = "".join(ch for ch in candidate if ch.isprintable())
    candidate = _UNSAFE_CHARS.sub("_", candidate)
    candidate = _LEADING_DOTS.sub("", candidate).strip("_. ")

    if not candidate:
        return ""

    stem, dot, suffix = candidate.rpartition(".")
    # Windows treats these as devices regardless of extension.
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if (stem or candidate).lower() in reserved:
        candidate = f"_{candidate}"

    if len(candidate) > 120:
        stem, dot, suffix = candidate.rpartition(".")
        if dot and len(suffix) <= 10:
            candidate = f"{stem[: 120 - len(suffix) - 1]}.{suffix}"
        else:
            candidate = candidate[:120]

    return candidate


class MediaManager:
    """Downloads, validates, and reaps Telegram media."""

    def __init__(
        self,
        gateway: TelegramGateway,
        settings: MediaSettings,
        *,
        root: Path | None = None,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._root = Path(root or settings.download_dir or Path.cwd() / "media")

    def run_directory(self, run_id: str) -> Path:
        """Per-run download directory, created on demand."""
        safe = sanitise_filename(run_id or "default", fallback="run")
        directory = self._root / safe
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # ------------------------------------------------------------ validate --
    def check_metadata(
        self,
        *,
        size: int | None,
        mime_type: str | None,
        file_name: str,
        media_type: str | None = None,
    ) -> None:
        """Reject a file *before* transferring it.

        ``media_type`` is the Telethon media class name. It is what distinguishes
        a kind that legitimately carries no MIME type of its own from a document
        that simply failed to declare one.
        """
        if size is not None and size > self._settings.max_file_bytes:
            raise MediaTooLarge(
                f"{file_name} is {size:,} bytes, over the "
                f"{self._settings.max_file_bytes:,}-byte limit."
            )

        suffix = Path(file_name).suffix.lower()
        if suffix and suffix in {e.lower() for e in self._settings.blocked_extensions}:
            raise MediaTypeRejected(
                f"Refusing to download {file_name}: the {suffix} extension is blocked."
            )

        allowed = self._settings.allowed_mime_prefixes
        if not allowed:
            return

        effective = (mime_type or "").strip() or _IMPLICIT_MIME_TYPES.get(media_type or "", "")
        if not effective:
            # Fail closed: an undeclared MIME type is not a permitted one, or the
            # allow-list would be bypassed by simply omitting the field.
            raise MediaTypeRejected(
                f"Refusing to download {file_name}: it declares no MIME type, so it "
                f"cannot be matched against the allow-list."
            )
        if not any(effective.lower().startswith(p.lower()) for p in allowed):
            raise MediaTypeRejected(
                f"Refusing to download {file_name}: MIME type {effective!r} is not on "
                f"the allow-list."
            )

    # ------------------------------------------------------------ download --
    async def download_message_media(
        self,
        peer: str | int,
        message_id: int,
        *,
        run_id: str,
        context: CallContext | None = None,
        file_name_override: str | None = None,
    ) -> DownloadResult:
        """Download the media attached to one message."""
        meta = await self._gateway.call(
            "get_messages",
            {"entity": peer, "ids": [int(message_id)]},
            context=context,
            projector=_project_media_metadata,
        )
        rows = meta.payload if isinstance(meta.payload, list) else []
        if not rows or not rows[0].get("has_media"):
            raise MediaError(f"Message {message_id} in {peer} has no downloadable media.")

        info = rows[0]
        file_name = sanitise_filename(
            file_name_override or info.get("file_name"),
            fallback=f"{peer}_{message_id}".replace("@", ""),
        )
        self.check_metadata(
            size=info.get("size"),
            mime_type=info.get("mime_type"),
            file_name=file_name,
            media_type=info.get("media_type"),
        )

        destination = self._unique_path(self.run_directory(run_id), file_name)

        written = await self._gateway.download_media(
            peer, int(message_id), str(destination), context=context
        )
        if not written:
            raise MediaError(f"Downloading media from message {message_id} produced no file.")

        path = Path(written)
        self._assert_inside_root(path)
        # Local metadata reads on a file this process just wrote. Offloading
        # them to a thread would cost more than the microseconds it saves.
        size = path.stat().st_size if path.exists() else 0  # noqa: ASYNC240

        if size > self._settings.max_file_bytes:
            # Metadata under-reported; delete rather than keep an oversized file.
            with contextlib.suppress(OSError):
                path.unlink()  # noqa: ASYNC240
            raise MediaTooLarge(
                f"{file_name} turned out to be {size:,} bytes, over the configured limit; "
                f"it has been deleted."
            )

        log.info(
            "media.downloaded",
            file_name=path.name,
            size_bytes=size,
            mime_type=info.get("mime_type"),
        )
        return DownloadResult(
            path=path,
            size_bytes=size,
            mime_type=info.get("mime_type"),
            file_name=path.name,
            message_id=int(message_id),
        )

    # ------------------------------------------------------------- cleanup --
    def cleanup(self, *, now: datetime | None = None) -> int:
        """Delete downloads older than the retention window. Returns the count."""
        days = self._settings.retention_days
        if days <= 0 or not self._root.exists():
            return 0

        cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        removed = 0

        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                log.warning("media.cleanup_failed", path=str(path), error=str(exc))

        for directory in sorted(self._root.rglob("*"), reverse=True):
            if not directory.is_dir():
                continue
            # A run directory is created before anything is written into it, and
            # Telethon does not mkdir. Reaping one that a live run is about to
            # use fails its download, so only directories too old to belong to a
            # live run are removed.
            with contextlib.suppress(OSError):
                if directory.stat().st_mtime < cutoff_ts:
                    directory.rmdir()  # only succeeds when empty

        if removed:
            log.info("media.cleaned", removed=removed, retention_days=days)
        return removed

    # ------------------------------------------------------------ internals --
    def _unique_path(self, directory: Path, file_name: str) -> Path:
        candidate = directory / file_name
        if not candidate.exists():
            return candidate
        stem, dot, suffix = file_name.rpartition(".")
        base = stem if dot else file_name
        extension = f".{suffix}" if dot else ""
        return directory / f"{base}_{int(time.time() * 1000)}{extension}"

    def _assert_inside_root(self, path: Path) -> None:
        """Last line of defence against a path escaping the download root."""
        try:
            resolved = path.resolve()
            root = self._root.resolve()
        except OSError as exc:
            raise MediaError(f"Could not verify the download path: {exc}") from exc
        if not resolved.is_relative_to(root):
            with contextlib.suppress(OSError):
                resolved.unlink()
            raise MediaError(
                f"Refusing a download that resolved outside the media directory: {resolved}"
            )


def _project_media_metadata(messages: Any) -> list[dict[str, Any]]:
    """Extract just what the size/type checks need."""
    from tgagent.telegram.serialize import _media_summary

    out: list[dict[str, Any]] = []
    for message in messages or []:
        media = getattr(message, "media", None)
        row: dict[str, Any] = {"id": getattr(message, "id", None), "has_media": media is not None}
        if media is not None:
            summary = _media_summary(media)
            row.update(
                {
                    "file_name": summary.get("file_name"),
                    "mime_type": summary.get("mime_type"),
                    "size": summary.get("size"),
                    "media_type": summary.get("type"),
                }
            )
        out.append(row)
    return out
