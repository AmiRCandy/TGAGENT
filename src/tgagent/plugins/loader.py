"""Turning installed plugins into tools the model can call.

What a plugin actually is
-------------------------
A directory with a ``plugin.toml`` and a Python module whose entry function
returns :class:`~tgagent.tools.base.Tool` objects. Those tools then sit in the
registry beside the built-in ones and are chosen by the model in the same way.

The trust boundary, stated plainly
----------------------------------
A plugin's code runs **in this process**, with everything this process can
reach: the session file, the API keys, the database. It is not the ``python``
sandbox, which holds no credentials and reaches Telegram only through a policed
pipe — a plugin is ordinary code with ordinary access, and no amount of wrapping
changes that.

So the decision point is *installation*, taken by the account owner explicitly,
recorded with the commit it came from, and reversible by switching the plugin off.
What the loader can still guarantee, and does:

* **Output is data.** Every result a plugin tool returns is fenced as untrusted
  regardless of what the plugin says, because it is coming from the internet and
  the model must not read it as instruction. A plugin author cannot opt out.
* **Calls are on the record.** Each invocation writes an audit entry with
  ``origin="plugin"``, so "what did that thing do?" has an answer.
* **Names cannot be stolen.** A plugin may not register a tool name that already
  exists, so it cannot shadow ``telegram_send_message`` with its own.
* **Failure is contained.** A plugin that raises on import, or whose
  requirements are missing, is reported and skipped; it never stops the agent
  from starting.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tgagent.observability.logging import get_logger
from tgagent.plugins.manifest import (
    PluginError,
    PluginManifest,
    missing_requirements,
    read_manifest,
)
from tgagent.plugins.registry import InstalledPlugin, PluginState
from tgagent.risk import RiskTier, TrustLevel
from tgagent.tools.base import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from tgagent.config.settings import Settings

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class PluginContext:
    """What a plugin is given when it builds its tools.

    Deliberately small. Anything a *tool* needs at call time — the Telegram
    gateway, the sandbox, the permission engine — arrives with the call in a
    :class:`~tgagent.tools.base.ToolContext`, which is also what keeps a plugin's
    Telegram access policed like everyone else's.
    """

    #: The plugin's own name, for log lines and error messages.
    name: str
    #: A writable directory of its own. Nothing else writes here.
    data_dir: Path
    #: Manifest defaults merged with whatever the operator set — API keys, hosts.
    config: dict[str, Any]
    #: Read-only view of the deployment's configuration.
    settings: Settings


@dataclass(slots=True)
class LoadedPlugin:
    """One plugin, and how loading it went."""

    manifest: PluginManifest
    installed: InstalledPlugin
    tools: list[Tool] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def load_plugins(settings: Settings, *, audit: Any = None) -> tuple[list[Tool], list[LoadedPlugin]]:
    """Every enabled plugin's tools, plus a report on all of them.

    Returns the report for disabled and broken plugins too: ``agent plugin list``
    has to be able to say *why* something is not working, and "it silently did
    not load" is the least useful answer available.
    """
    state = PluginState(settings.data_dir)
    installed = state.all()
    report: list[LoadedPlugin] = []
    tools: list[Tool] = []
    taken = _reserved_tool_names(settings)

    for manifest, record in _discover(settings, state, installed):
        entry = LoadedPlugin(manifest=manifest, installed=record)
        report.append(entry)

        if not record.enabled:
            entry.error = "switched off"
            continue
        if missing := missing_requirements(manifest):
            entry.error = f"needs {', '.join(missing)} — pip install {' '.join(missing)}"
            log.warning("plugins.requirements_missing", plugin=manifest.name, missing=missing)
            continue

        try:
            built = _build(manifest, record, settings, state)
        except PluginError as exc:
            entry.error = str(exc)
            log.error("plugins.load_failed", plugin=manifest.name, error=str(exc))
            continue
        except Exception as exc:
            entry.error = f"{type(exc).__name__}: {exc}"
            log.error("plugins.load_crashed", plugin=manifest.name, error=str(exc), exc_info=True)
            continue

        kept: list[Tool] = []
        for tool in built:
            if tool.name in taken:
                entry.error = (
                    f"tool {tool.name!r} is already provided by something else; "
                    f"rename it in the plugin"
                )
                log.error("plugins.name_collision", plugin=manifest.name, tool=tool.name)
                continue
            taken.add(tool.name)
            kept.append(_guard(tool, manifest, audit))

        if len(kept) > settings.plugins.max_tools_per_plugin:
            entry.error = (
                f"declares {len(kept)} tools; the limit is {settings.plugins.max_tools_per_plugin}"
            )
            continue

        entry.tools = kept
        tools.extend(kept)
        log.info(
            "plugins.loaded",
            plugin=manifest.name,
            version=manifest.version,
            tools=[tool.name for tool in kept],
        )

    return tools, report


def _discover(
    settings: Settings, state: PluginState, installed: dict[str, InstalledPlugin]
) -> list[tuple[PluginManifest, InstalledPlugin]]:
    """Built-ins first, then whatever is in the plugins directory.

    A built-in is listed even when the state file has never heard of it, so a
    fresh install has the shipped plugins available without a setup step — and
    switching one off is still just a row in the file.
    """
    from tgagent.plugins.builtin import builtin_manifests

    found: list[tuple[PluginManifest, InstalledPlugin]] = []
    seen: set[str] = set()

    for manifest in builtin_manifests():
        record = installed.get(manifest.name) or InstalledPlugin(
            name=manifest.name,
            source="builtin",
            enabled=settings.plugins.builtins_enabled,
        )
        found.append((manifest, record))
        seen.add(manifest.name)

    directory = state.plugins_dir
    if directory.is_dir():
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child.name in seen:
                continue
            try:
                manifest = read_manifest(child)
            except PluginError as exc:
                log.warning("plugins.unreadable", path=str(child), error=str(exc))
                continue
            record = installed.get(manifest.name) or InstalledPlugin(
                name=manifest.name, source=str(child), enabled=False
            )
            found.append((manifest, record))
            seen.add(manifest.name)

    return found


def _build(
    manifest: PluginManifest,
    record: InstalledPlugin,
    settings: Settings,
    state: PluginState,
) -> Sequence[Tool]:
    """Import the plugin and ask it for its tools."""
    data_dir = state.directory_for(manifest.name) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    context = PluginContext(
        name=manifest.name,
        data_dir=data_dir,
        config={**manifest.config, **record.config},
        settings=settings,
    )

    module = (
        _import_builtin(manifest)
        if manifest.builtin
        else _import_from_directory(manifest, state.directory_for(manifest.name))
    )

    factory = getattr(module, manifest.factory_name, None)
    if not callable(factory):
        raise PluginError(
            f"{manifest.name}: {manifest.entry} is not callable. The entry point must "
            f"be a function taking a PluginContext and returning a list of tools."
        )

    built = factory(context)
    if not isinstance(built, list | tuple):
        raise PluginError(
            f"{manifest.name}: {manifest.factory_name}() must return a list of tools."
        )
    for tool in built:
        if not isinstance(tool, Tool):
            raise PluginError(
                f"{manifest.name}: {type(tool).__name__} is not a tool — it needs name, "
                f"description, parameters, risk_hint, and an async run()."
            )
    return built


def _import_builtin(manifest: PluginManifest) -> Any:
    from importlib import import_module

    return import_module(f"tgagent.plugins.builtin.{manifest.module_name}")


def _import_from_directory(manifest: PluginManifest, directory: Path) -> Any:
    """Import a plugin's module from its own directory.

    Loaded under a namespaced module name so two plugins with a ``main.py`` do
    not overwrite each other in ``sys.modules``, and with the plugin's directory
    on the path only for the duration of the import so its private modules
    resolve without leaking onto everyone else's import path.
    """
    path = directory / f"{manifest.module_name.replace('.', '/')}.py"
    if not path.exists():
        raise PluginError(f"{manifest.name}: {path.name} does not exist in the plugin.")

    module_name = f"tgagent_plugin_{manifest.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{manifest.name}: cannot import {path}.")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    inserted = str(directory)
    sys.path.insert(0, inserted)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(
            f"{manifest.name}: importing it raised {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        with_path = [entry for entry in sys.path if entry != inserted]
        sys.path[:] = with_path
    return module


def _reserved_tool_names(settings: Settings) -> set[str]:
    """Tool names a plugin may not take, which is all the built-in ones."""
    from tgagent.tools import build_core_registry

    return set(build_core_registry(settings).names())


def _guard(tool: Tool, manifest: PluginManifest, audit: Any) -> Tool:
    """Wrap a plugin tool with the two things it does not get to decide."""
    return _GuardedTool(tool, manifest, audit)


class _GuardedTool:
    """A plugin's tool, with its output fenced and its calls recorded.

    Wrapping rather than trusting: a plugin author cannot mark internet content
    as trusted, cannot silence the audit entry, and cannot make a failure look
    like a success. The tool's own name, description, and schema are passed
    through untouched — those are the parts it is *for*.
    """

    def __init__(self, inner: Tool, manifest: PluginManifest, audit: Any) -> None:
        self._inner = inner
        self._manifest = manifest
        self._audit = audit
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        # Advisory, like every other risk hint; the gateway still decides for
        # anything that reaches Telegram.
        self.risk_hint = getattr(inner, "risk_hint", RiskTier.READ_ONLY)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = datetime.now(UTC)
        try:
            result = await self._inner.run(arguments, context)
        except Exception as exc:
            log.error(
                "plugins.tool_failed",
                plugin=self._manifest.name,
                tool=self.name,
                error=str(exc),
                exc_info=True,
            )
            await self._record(context, arguments, started, error=str(exc))
            return ToolResult.error(f"The {self._manifest.name} plugin's {self.name} failed: {exc}")

        await self._record(context, arguments, started, error=None)
        # Fenced whatever the plugin said. Search results, page text and video
        # titles are written by strangers; the runtime must see them as data.
        return ToolResult(
            content=result.content,
            is_error=result.is_error,
            trust=TrustLevel.UNTRUSTED,
            source=f"plugin:{self._manifest.name}/{self.name}",
            metadata={**result.metadata, "plugin": self._manifest.name},
        )

    async def _record(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
        started: datetime,
        *,
        error: str | None,
    ) -> None:
        if self._audit is None:
            return
        from tgagent.storage.models import AuditEntry

        payload = repr(sorted(arguments)).encode()
        try:
            await self._audit.record(
                AuditEntry(
                    run_id=context.run_id,
                    conversation_id=context.conversation_id,
                    method=f"plugin.{self._manifest.name}.{self.name}",
                    risk=self.risk_hint.value,
                    decision="allow",
                    target=self._manifest.name,
                    argument_digest=hashlib.sha256(payload).hexdigest()[:16],
                    succeeded=error is None,
                    error=error,
                    duration_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
                    origin="plugin",
                )
            )
        except Exception as exc:  # noqa: BLE001 - auditing must not break a call
            log.error("plugins.audit_failed", plugin=self._manifest.name, error=str(exc))


def ensure_record(state: PluginState, name: str, *, settings: Settings) -> InstalledPlugin:
    """The state row for *name*, created if this is a built-in with none yet.

    A shipped plugin works without ever appearing in ``plugins.json`` — absence
    means "as it comes". The first time somebody switches one off or configures
    it, that default has to become a row, or the change has nowhere to live and
    the command fails with "not installed" about a plugin that plainly is.
    """
    from tgagent.plugins.builtin import builtin_manifests

    if (existing := state.get(name)) is not None:
        return existing
    if any(manifest.name == name for manifest in builtin_manifests()):
        return state.put(
            InstalledPlugin(name=name, source="builtin", enabled=settings.plugins.builtins_enabled)
        )
    raise PluginError(
        f"No plugin named {name!r}. `plugin list` shows what is installed, and "
        f"`plugin add <url>` installs one."
    )


__all__ = ["LoadedPlugin", "PluginContext", "ensure_record", "load_plugins"]
