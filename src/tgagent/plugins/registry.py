"""Which plugins are installed, and which are switched on.

State lives in ``<data_dir>/plugins.json`` beside the database — a file the
operator can read, diff, and delete. That matters more here than anywhere else
in the project: this file is the record of *what code has been added to the
account*, so it is kept inspectable rather than hidden in a table.

Enabling is deliberately separate from installing. Installing fetches code;
enabling is what lets it run. Both are the owner's decision, and a plugin that
turns out to be a mistake can be switched off without deleting anything, which is
what you want at the moment you are unsure.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tgagent.observability.logging import get_logger
from tgagent.plugins.manifest import PluginError

log = get_logger(__name__)

STATE_NAME = "plugins.json"


@dataclass(slots=True)
class InstalledPlugin:
    """One row of the state file."""

    name: str
    #: ``builtin``, or the URL it was installed from.
    source: str
    enabled: bool = True
    #: The commit actually installed, so "what is running" has an answer that
    #: survives the upstream branch moving.
    ref: str = ""
    installed_at: str = ""
    #: Operator-set values the plugin reads through ``PluginContext.config`` —
    #: an API key for a search backend, say.
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def builtin(self) -> bool:
        return self.source == "builtin"

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "ref": self.ref,
            "installed_at": self.installed_at,
            "config": self.config,
        }


class PluginState:
    """Reads and writes ``plugins.json``."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir).expanduser()
        self._path = self._dir / STATE_NAME

    @property
    def path(self) -> Path:
        return self._path

    @property
    def plugins_dir(self) -> Path:
        """Where installed plugins are unpacked."""
        return self._dir / "plugins"

    def directory_for(self, name: str) -> Path:
        return self.plugins_dir / name

    # ----------------------------------------------------------------- read ---
    def all(self) -> dict[str, InstalledPlugin]:
        """Everything the file mentions. A broken file reads as empty.

        Refusing to start because this file is malformed would be the wrong
        trade: it holds optional extras, and the agent's own capabilities do not
        depend on any of them.
        """
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("plugins.state_unreadable", path=str(self._path), error=str(exc))
            return {}
        if not isinstance(raw, dict):
            return {}

        found: dict[str, InstalledPlugin] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            found[str(name)] = InstalledPlugin(
                name=str(name),
                source=str(entry.get("source", "")),
                enabled=bool(entry.get("enabled", True)),
                ref=str(entry.get("ref", "")),
                installed_at=str(entry.get("installed_at", "")),
                config=dict(entry.get("config") or {}),
            )
        return found

    def get(self, name: str) -> InstalledPlugin | None:
        return self.all().get(name)

    # ---------------------------------------------------------------- write ---
    def put(self, plugin: InstalledPlugin) -> InstalledPlugin:
        if not plugin.installed_at:
            plugin.installed_at = datetime.now(UTC).isoformat()
        current = self.all()
        current[plugin.name] = plugin
        self._write(current)
        return plugin

    def set_enabled(self, name: str, enabled: bool) -> InstalledPlugin:
        current = self.all()
        plugin = current.get(name)
        if plugin is None:
            raise PluginError(f"No plugin named {name!r} is installed.")
        plugin.enabled = enabled
        self._write(current)
        log.warning("plugins.toggled", plugin=name, enabled=enabled)
        return plugin

    def set_config(self, name: str, values: dict[str, Any]) -> InstalledPlugin:
        current = self.all()
        plugin = current.get(name)
        if plugin is None:
            raise PluginError(f"No plugin named {name!r} is installed.")
        plugin.config.update(values)
        self._write(current)
        return plugin

    def forget(self, name: str) -> bool:
        current = self.all()
        if current.pop(name, None) is None:
            return False
        self._write(current)
        return True

    def _write(self, plugins: dict[str, InstalledPlugin]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        body = {name: plugin.to_json() for name, plugin in sorted(plugins.items())}
        try:
            self._path.write_text(
                json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            # A plugin's config may hold an API key, so the file gets the same
            # treatment as the other credential-bearing files in this directory.
            with contextlib.suppress(OSError, NotImplementedError):
                self._path.chmod(0o600)
        except OSError as exc:
            raise PluginError(f"Cannot write {self._path}: {exc}") from exc


__all__ = ["STATE_NAME", "InstalledPlugin", "PluginState"]
