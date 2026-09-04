"""The youtube plugin: look at a video, or download one.

`youtube_info` reads metadata and needs nothing but the network. `youtube_download`
writes a file into the media directory, where the existing Telegram tools can
send it — the two halves are separate because "how long is this video" should not
cost a 200 MB download.

Both need `yt-dlp`, which is not a dependency of tgagent: it moves fast, it is
large, and most people never download a video. The manifest declares it, so the
plugin reports itself unavailable with the exact pip command rather than failing
at the moment somebody asks.

What it will not do
-------------------
Refuse first, download second. Live streams, playlists, and anything past
`max_duration_seconds` are declined before a byte moves, because the failure mode
here is a 1 GB file on a 1 GB VPS and a wedged event loop. Downloading is also
somebody else's copyright much of the time; the tool description says so and the
limits are the enforcement.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tgagent.plugins.loader import PluginContext
from tgagent.risk import RiskTier
from tgagent.tools.base import (
    ToolContext,
    ToolResult,
    boolean_field,
    object_schema,
    require,
    string_field,
)

_DEFAULT_MAX_DURATION = 1800  # 30 minutes
_DEFAULT_MAX_MB = 200
_TIMEOUT = 600.0


class YoutubeInfoTool:
    name = "youtube_info"
    description = (
        "Read a video's title, channel, duration, and description without downloading it. "
        "Use this first: it answers most questions about a link, and it is what tells you "
        "whether a download would be refused for length."
    )
    risk_hint = RiskTier.READ_ONLY
    parameters = object_schema({"url": string_field("The video URL.")}, required=["url"])

    def __init__(self, context: PluginContext) -> None:
        self._context = context

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(require(arguments, "url", self.name)).strip()
        try:
            info = await _probe(url)
        except _YoutubeError as exc:
            return ToolResult.error(str(exc))

        return ToolResult(
            content=json.dumps(
                {
                    "title": info.get("title"),
                    "channel": info.get("uploader") or info.get("channel"),
                    "duration_seconds": info.get("duration"),
                    "upload_date": info.get("upload_date"),
                    "view_count": info.get("view_count"),
                    "is_live": bool(info.get("is_live")),
                    "description": (info.get("description") or "")[:2000],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


class YoutubeDownloadTool:
    name = "youtube_download"
    description = (
        "Download a video, or just its audio, into the media directory and return the "
        "path — then telegram_send_message's sibling upload tools can send it. Check "
        "youtube_info first: anything live, a playlist, or longer than the configured "
        "limit is refused before the download starts. Downloading other people's videos "
        "is often their copyright; say so if the request looks like redistribution."
    )
    risk_hint = RiskTier.REVERSIBLE
    parameters = object_schema(
        {
            "url": string_field("The video URL."),
            "audio_only": boolean_field(
                "Extract the audio track instead of the video. Much smaller.", default=False
            ),
        },
        required=["url"],
    )

    def __init__(self, context: PluginContext) -> None:
        self._context = context

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(require(arguments, "url", self.name)).strip()
        audio_only = bool(arguments.get("audio_only"))
        config = self._context.config
        max_duration = int(config.get("max_duration_seconds") or _DEFAULT_MAX_DURATION)
        max_mb = int(config.get("max_megabytes") or _DEFAULT_MAX_MB)

        try:
            info = await _probe(url)
        except _YoutubeError as exc:
            return ToolResult.error(str(exc))

        if info.get("is_live"):
            return ToolResult.error("That is a live stream, which has no end to download.")
        duration = int(info.get("duration") or 0)
        if duration and duration > max_duration:
            return ToolResult.error(
                f"That video is {duration // 60} minutes long and the limit is "
                f"{max_duration // 60}. The owner can raise it with: agent plugin set "
                f"youtube max_duration_seconds <n>"
            )

        media = self._context.settings.media.download_dir or self._context.data_dir
        target = Path(media) / "youtube"
        target.mkdir(parents=True, exist_ok=True)

        try:
            path = await _download(url, target, audio_only=audio_only, max_mb=max_mb)
        except _YoutubeError as exc:
            return ToolResult.error(str(exc))

        size_mb = path.stat().st_size / (1024 * 1024)
        return ToolResult(
            content=json.dumps(
                {
                    "path": str(path),
                    "megabytes": round(size_mb, 1),
                    "title": info.get("title"),
                    "audio_only": audio_only,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={"path": str(path), "bytes": path.stat().st_size},
        )


def build_tools(context: PluginContext) -> list[Any]:
    """The plugin's entry point."""
    return [YoutubeInfoTool(context), YoutubeDownloadTool(context)]


# ------------------------------------------------------------------ yt-dlp ---
class _YoutubeError(Exception):
    """Something yt-dlp could not do, in words the model can act on."""


def _ytdlp() -> Any:
    try:
        import yt_dlp  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - declared in the manifest
        raise _YoutubeError(
            "yt-dlp is not installed, so this plugin cannot do anything. Install it with "
            "`pip install yt-dlp` in the same environment as tgagent."
        ) from exc
    return yt_dlp


async def _probe(url: str) -> dict[str, Any]:
    """Metadata only — no formats downloaded.

    In a thread because yt-dlp is synchronous and this is a network call: on the
    event loop it would stall every other chat for its duration.
    """
    module = _ytdlp()

    def extract() -> dict[str, Any]:
        options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with module.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        if info and info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise _YoutubeError("That URL is a playlist with nothing in it.")
            raise _YoutubeError(
                f"That is a playlist of {len(entries)} videos. Give me one video's URL."
            )
        return dict(info or {})

    try:
        return await asyncio.wait_for(asyncio.to_thread(extract), timeout=120.0)
    except TimeoutError as exc:
        raise _YoutubeError("Reading that video's details timed out.") from exc
    except _YoutubeError:
        raise
    except Exception as exc:
        raise _YoutubeError(f"yt-dlp could not read that URL: {exc}") from exc


async def _download(url: str, into: Path, *, audio_only: bool, max_mb: int) -> Path:
    module = _ytdlp()
    template = str(into / "%(title).80s-%(id)s.%(ext)s")

    def fetch() -> Path:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": template,
            # yt-dlp checks this against the format's reported size and skips
            # rather than filling the disk.
            "max_filesize": max_mb * 1024 * 1024,
            "format": "bestaudio/best" if audio_only else "best[height<=720]/best",
        }
        if audio_only:
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
        with module.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            name = downloader.prepare_filename(info)
        candidate = Path(name)
        if not candidate.exists():
            # A postprocessor changes the extension; take the newest match.
            matches = sorted(
                into.glob(f"{candidate.stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if not matches:
                raise _YoutubeError(
                    f"The download produced no file — it may be larger than the {max_mb} MB limit."
                )
            candidate = matches[0]
        return candidate

    try:
        return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=_TIMEOUT)
    except TimeoutError as exc:
        raise _YoutubeError(f"The download ran past {_TIMEOUT / 60:.0f} minutes.") from exc
    except _YoutubeError:
        raise
    except Exception as exc:
        raise _YoutubeError(f"yt-dlp failed: {exc}") from exc
