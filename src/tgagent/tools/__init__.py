"""The tool layer.

Three tiers, described in ``docs/tool-architecture.md``:

1. curated ``telegram_*`` tools for the common operations;
2. ``telegram_api_search`` to discover the rest of the API surface offline;
3. ``python`` (sandboxed) and ``telegram_invoke`` to reach all of it.

Plus a fourth thing that is not a tier: plugins, which add tools *beside* those
rather than deeper into Telegram — see :mod:`tgagent.plugins`. They are built
separately from :func:`build_core_registry` because the plugin loader has to know
which names are already taken, and asking the registry that would be circular.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tgagent.tools.base import (
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from tgagent.config.settings import Settings

__all__ = [
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "build_core_registry",
    "build_default_registry",
    "register_plugin_tools",
]


def build_core_registry(settings: Settings) -> ToolRegistry:
    """The tools that ship with the agent itself, without plugins.

    Tools are omitted rather than left in and made to fail: a tool the model can
    see is a tool it will try, and a disabled capability advertised in the schema
    wastes tokens and invites confusion.
    """
    from tgagent.tools.autoreply_tools import build_autoreply_tools
    from tgagent.tools.code_tool import PythonTool
    from tgagent.tools.docs_tool import ApiSearchTool
    from tgagent.tools.memory_tools import build_memory_tools
    from tgagent.tools.schedule_tools import build_schedule_tools
    from tgagent.tools.telegram_tools import build_telegram_tools

    registry = ToolRegistry()
    registry.register_all(build_telegram_tools())
    registry.register(ApiSearchTool())

    if settings.features.code_execution and settings.sandbox.backend != "disabled":
        registry.register(PythonTool())
    if settings.features.memory:
        registry.register_all(build_memory_tools())
    if settings.features.scheduling:
        registry.register_all(build_schedule_tools())
    # Off by default, and omitted entirely when it is off: a model that can see
    # autoreply_start will offer it, and being offered something the deployment
    # has deliberately switched off is worse than not knowing it exists.
    if settings.autoreply.enabled:
        registry.register_all(build_autoreply_tools())
    if not settings.features.media_download:
        registry.unregister("telegram_download_media")

    return registry


def register_plugin_tools(
    registry: ToolRegistry, settings: Settings, *, audit: Any = None
) -> list[Any]:
    """Add every enabled plugin's tools to *registry*, in place.

    Returns the loader's report — including the plugins that did not load and
    why — because "installed but doing nothing" is a state somebody has to be
    able to see. Called again after an install or a toggle, so the model's tool
    list changes without restarting the process.
    """
    from tgagent.plugins import load_plugins

    if not settings.plugins.enabled:
        return []

    tools, report = load_plugins(settings, audit=audit)
    for tool in tools:
        # replace=True: a reload re-registers the same names deliberately.
        registry.register(tool, replace=True)
    return report


def build_default_registry(settings: Settings, *, audit: Any = None) -> ToolRegistry:
    """Everything this deployment offers the model: core tools plus plugins."""
    registry = build_core_registry(settings)
    register_plugin_tools(registry, settings, audit=audit)
    return registry
