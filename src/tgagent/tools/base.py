"""Tool interfaces.

A tool is a named capability with a JSON Schema, a description written for the
model, and an async implementation. The registry turns the set of registered
tools into the ``tools`` array the LLM layer sends.

Two things here are load-bearing for security:

* :attr:`ToolResult.trust` marks output that came from outside the system.
  The runtime fences anything marked ``UNTRUSTED`` before it reaches the model,
  so a tool author cannot forget to do it.
* :attr:`Tool.risk_hint` is documentation and UI affordance only. Actual
  authorisation happens in the gateway, per Telegram call. A tool cannot grant
  itself permission by declaring a low risk hint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tgagent.errors import ToolInputError, ToolNotFound
from tgagent.llm.base import ToolSpec
from tgagent.risk import RiskTier, TrustLevel

if TYPE_CHECKING:  # pragma: no cover
    from tgagent.config.settings import Settings
    from tgagent.sandbox.base import SandboxRunner
    from tgagent.storage.base import MemoryRepository, TaskRepository
    from tgagent.telegram.gateway import TelegramGateway
    from tgagent.telegram.history import HistoryReader
    from tgagent.telegram.media import MediaManager
    from tgagent.telegram.schema import TelegramSchemaIndex


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to reach.

    Passing this explicitly — rather than letting tools import singletons — is
    what makes tools testable in isolation and keeps the dependency graph honest.
    """

    run_id: str
    settings: Settings
    conversation_id: str | None = None
    #: Whether a human can answer confirmation prompts during this run.
    interactive: bool = True

    gateway: TelegramGateway | None = None
    history: HistoryReader | None = None
    media: MediaManager | None = None
    schema: TelegramSchemaIndex | None = None
    sandbox: SandboxRunner | None = None
    memory: MemoryRepository | None = None
    tasks: TaskRepository | None = None

    #: Set when the user cancels; long-running tools should check it.
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def require_gateway(self) -> TelegramGateway:
        if self.gateway is None:
            raise ToolInputError(
                "Telegram is not connected in this run, so that tool is unavailable."
            )
        return self.gateway

    def require_history(self) -> HistoryReader:
        if self.history is None:
            raise ToolInputError("Telegram is not connected in this run.")
        return self.history

    def call_context(self) -> Any:
        """Build the gateway :class:`CallContext` for this run."""
        from tgagent.telegram.gateway import CallContext

        return CallContext(
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            origin="tool",
            interactive=self.interactive,
        )


@dataclass(slots=True)
class ToolResult:
    """What a tool hands back to the runtime."""

    #: Rendered for the model. Already truncated by the tool if large.
    content: str
    is_error: bool = False
    #: ``UNTRUSTED`` content is fenced by the runtime before the model sees it.
    trust: TrustLevel = TrustLevel.AGENT
    #: Provenance for the fence, e.g. ``telegram:chat/@alex``.
    source: str = "tool"
    #: Not shown to the model; used by interfaces and the audit trail.
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **metadata: Any) -> ToolResult:
        return cls(content=message, is_error=True, metadata=metadata)

    @classmethod
    def untrusted(cls, content: str, *, source: str, **metadata: Any) -> ToolResult:
        return cls(
            content=content, trust=TrustLevel.UNTRUSTED, source=source, metadata=metadata
        )


@runtime_checkable
class Tool(Protocol):
    """A capability the model can invoke."""

    name: str
    description: str
    #: JSON Schema for the arguments object.
    parameters: dict[str, Any]
    #: Advisory only — the gateway is what actually authorises.
    risk_hint: RiskTier

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...


class ToolRegistry:
    """Holds the active tool set and renders it for the model."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"A tool named {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "(none)"
            raise ToolNotFound(f"No tool named {name!r}. Available tools: {available}.")
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        """The tool list handed to the provider, in a stable order.

        Order is stable so the serialised tool array is byte-identical between
        requests, which is what lets provider-side prompt caching work.
        """
        return [
            ToolSpec(
                name=tool.name, description=tool.description, parameters=tool.parameters
            )
            for tool in (self._tools[name] for name in sorted(self._tools))
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


# --------------------------------------------------------------- helpers ----
def string_field(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def integer_field(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def boolean_field(description: str, *, default: bool | None = None) -> dict[str, Any]:
    field_schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        field_schema["default"] = default
    return field_schema


def object_schema(
    properties: dict[str, Any], *, required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def require(arguments: dict[str, Any], name: str, tool: str) -> Any:
    """Fetch a required argument, or raise a message the model can act on."""
    if name not in arguments or arguments[name] in (None, ""):
        raise ToolInputError(f"{tool}: the {name!r} argument is required.")
    return arguments[name]


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
