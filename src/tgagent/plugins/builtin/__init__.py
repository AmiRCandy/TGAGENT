"""Plugins that ship with tgagent.

They go through the same loader, the same enable/disable state, and the same
output fencing as anything installed from a URL — the only difference is that
their code is already here, so there is nothing to fetch and nothing to trust
that you did not already trust by installing tgagent.

Their manifests are declared in code rather than as ``plugin.toml`` files
because a manifest inside the package would have to be shipped as package data,
and a plugin that works from a git checkout but not from a wheel is a worse
problem than a little duplication. Third-party plugins use the file.
"""

from __future__ import annotations

from tgagent.plugins.manifest import PluginManifest, parse_manifest

#: Every built-in, in the order they are offered.
_BUILTINS: tuple[dict[str, object], ...] = (
    {
        "plugin": {
            "name": "web-search",
            "version": "1.0.0",
            "description": "Search the web, and read a page as text.",
            "entry": "websearch:build_tools",
            "requires": ["httpx"],
            "tools": ["web_search", "web_fetch"],
            "homepage": "https://github.com/AmiRCandy/tgagent/blob/main/docs/plugins.md",
            # Searching needs a provider key; fetching does not. Set with:
            #   agent plugin set web-search api_key <key>
            "config": {"provider": "brave", "api_key": ""},
        }
    },
    {
        "plugin": {
            "name": "youtube",
            "version": "1.0.0",
            "description": "Read a video's details, or download it to the media directory.",
            "entry": "youtube:build_tools",
            "requires": ["yt_dlp"],
            "tools": ["youtube_info", "youtube_download"],
            "homepage": "https://github.com/AmiRCandy/tgagent/blob/main/docs/plugins.md",
            "config": {"max_duration_seconds": 1800, "max_megabytes": 200},
        }
    },
)


def builtin_manifests() -> list[PluginManifest]:
    """The shipped plugins, validated by the same parser as everyone else's."""
    return [
        parse_manifest(raw, source=f"builtin:{raw['plugin']['name']}", builtin=True)  # type: ignore[index]
        for raw in _BUILTINS
    ]


__all__ = ["builtin_manifests"]
