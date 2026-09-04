"""What a plugin declares about itself.

A manifest is the contract between a plugin and this project: a name, an entry
point, what it needs importable, and which tools it intends to add. It is read
*before* any of the plugin's code is imported, which is the only order that lets
an install be refused on inspection rather than on execution.

The file is TOML because it is unambiguous, has no execution semantics, and is in
the standard library since 3.11 — a plugin manifest that could run code would
defeat the point of reading it first.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tgagent.errors import TgAgentError

MANIFEST_NAME = "plugin.toml"

#: A slug is a directory name, a command argument, and a key in a JSON file, so
#: it is kept to the intersection of what all three handle without quoting.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
#: Tool names reach the model's tool array, where the API requires this shape.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
#: ``module:function`` — a dotted module path inside the plugin, and a callable.
_ENTRY = re.compile(r"^([a-zA-Z_][\w.]*):([a-zA-Z_]\w*)$")


class PluginError(TgAgentError):
    """A plugin could not be read, loaded, or trusted.

    Every message is written for whoever is about to fix it — usually somebody
    writing their first plugin, occasionally somebody installing one that turned
    out to be broken.
    """

    user_message = "A plugin could not be loaded."


@dataclass(slots=True, frozen=True)
class PluginManifest:
    """A validated ``plugin.toml``."""

    name: str
    version: str
    description: str
    #: ``module:function``, resolved relative to the plugin's own directory.
    entry: str
    #: Import names that must already be available. Checked, never installed:
    #: a plugin that could run `pip` at install time would be a second, quieter
    #: way to execute arbitrary code.
    requires: tuple[str, ...] = ()
    #: The tools this plugin says it adds. Declared so a name collision can be
    #: refused before the module is imported, and so `plugin info` can answer
    #: without loading anything.
    tools: tuple[str, ...] = ()
    homepage: str = ""
    #: Set for the plugins that ship in this repository.
    builtin: bool = False
    #: Free-form defaults the plugin reads through ``PluginContext.config``.
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def module_name(self) -> str:
        """The importable half of ``entry``."""
        return self.entry.split(":", 1)[0]

    @property
    def factory_name(self) -> str:
        """The callable half of ``entry``."""
        return self.entry.split(":", 1)[1]

    def summary(self) -> str:
        return f"{self.name} {self.version} — {self.description}"


def parse_manifest(data: dict[str, Any], *, source: str, builtin: bool = False) -> PluginManifest:
    """Validate a manifest mapping, or say exactly what is wrong with it.

    Every message names the file and the field, because the reader is somebody
    writing their first plugin and a bare "invalid manifest" tells them nothing.
    """
    section = data.get("plugin")
    if not isinstance(section, dict):
        raise PluginError(f"{source}: needs a [plugin] section.")

    def text(key: str, *, required: bool = True, default: str = "") -> str:
        value = section.get(key, default)
        if not isinstance(value, str) or (required and not value.strip()):
            raise PluginError(f"{source}: [plugin] {key} must be a non-empty string.")
        return value.strip()

    name = text("name")
    if not _SLUG.match(name):
        raise PluginError(
            f"{source}: [plugin] name must be lowercase letters, digits and hyphens "
            f"(3-40 characters); got {name!r}."
        )

    entry = text("entry")
    if not _ENTRY.match(entry):
        raise PluginError(
            f"{source}: [plugin] entry must look like 'module:function' — the module "
            f"is imported from the plugin's own directory and the function returns its "
            f"tools; got {entry!r}."
        )

    tools = _string_list(section.get("tools", ()), key="tools", source=source)
    for tool in tools:
        if not _TOOL_NAME.match(tool):
            raise PluginError(
                f"{source}: tool name {tool!r} is not usable — lowercase letters, "
                f"digits and underscores only, 3-64 characters."
            )

    config = section.get("config", {})
    if not isinstance(config, dict):
        raise PluginError(f"{source}: [plugin] config must be a table.")

    return PluginManifest(
        name=name,
        version=text("version", default="0.0.0", required=False) or "0.0.0",
        description=text("description", default="", required=False),
        entry=entry,
        requires=_string_list(section.get("requires", ()), key="requires", source=source),
        tools=tools,
        homepage=text("homepage", default="", required=False),
        builtin=builtin,
        config=dict(config),
    )


def read_manifest(directory: Path) -> PluginManifest:
    """Read and validate the manifest in *directory*."""
    path = directory / MANIFEST_NAME
    if not path.exists():
        raise PluginError(
            f"{directory.name} has no {MANIFEST_NAME}. Every plugin needs one at the "
            f"root of its repository — see docs/plugins.md."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"{path}: cannot be read as TOML: {exc}") from exc
    return parse_manifest(raw, source=str(path))


def missing_requirements(manifest: PluginManifest) -> list[str]:
    """Which of the plugin's requirements are not importable here.

    Checked rather than installed. Downloading code is already a decision the
    operator makes deliberately; running an installer on its behalf would add a
    second one they never made.
    """
    from importlib.util import find_spec

    missing: list[str] = []
    for requirement in manifest.requires:
        module = requirement.split("[")[0].replace("-", "_")
        try:
            if find_spec(module) is None:
                missing.append(requirement)
        except (ImportError, ValueError):
            missing.append(requirement)
    return missing


def _string_list(value: Any, *, key: str, source: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise PluginError(f"{source}: [plugin] {key} must be a list of strings.")
    return tuple(item.strip() for item in value if item.strip())


__all__ = [
    "MANIFEST_NAME",
    "PluginError",
    "PluginManifest",
    "missing_requirements",
    "parse_manifest",
    "read_manifest",
]
