"""Plugins: tools somebody else wrote.

The agent's own tools cover Telegram. A plugin adds capability *beside* that —
web search, a downloader, an internal API at work — as ordinary tools the model
chooses between in the usual way.

Read :mod:`tgagent.plugins.loader` before writing one, and ``docs/plugins.md``
before installing one. The short version: a plugin's code runs in this process
with this account's credentials, so installing one is a decision of the same size
as installing tgagent itself. What the loader still guarantees is that a plugin's
output is fenced as untrusted, its calls are audited, and it cannot take the name
of a built-in tool.
"""

from tgagent.plugins.install import Installed, install, normalise_url, remove
from tgagent.plugins.loader import (
    LoadedPlugin,
    PluginContext,
    ensure_record,
    load_plugins,
)
from tgagent.plugins.manifest import (
    MANIFEST_NAME,
    PluginError,
    PluginManifest,
    missing_requirements,
    parse_manifest,
    read_manifest,
)
from tgagent.plugins.registry import STATE_NAME, InstalledPlugin, PluginState

__all__ = [
    "MANIFEST_NAME",
    "STATE_NAME",
    "Installed",
    "InstalledPlugin",
    "LoadedPlugin",
    "PluginContext",
    "PluginError",
    "PluginManifest",
    "PluginState",
    "ensure_record",
    "install",
    "load_plugins",
    "missing_requirements",
    "normalise_url",
    "parse_manifest",
    "read_manifest",
    "remove",
]
