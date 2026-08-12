"""The RPC bridge: sandbox requests → the policed gateway.

This is the only thing on the host side that a sandboxed program can reach, and
it is intentionally thin — every decision it could make is one the gateway
already makes better. Its job is to:

* forward the call, tagged with ``origin="sandbox"`` so the audit trail
  distinguishes generated code from curated tool use;
* count calls against a per-execution budget, so a runaway loop cannot make ten
  thousand requests;
* return the gateway's *serialised payload*, never a live object.

Note what is deliberately absent: no allow-list of methods lives here. The
sandbox is allowed to *ask* for anything, exactly as a tool is; the permission
engine decides. Duplicating policy in a second place is how the two copies drift
apart and one of them gets it wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tgagent.errors import SandboxError
from tgagent.observability.logging import get_logger
from tgagent.security.injection import ScanResult
from tgagent.telegram.gateway import CallContext, TelegramGateway

log = get_logger(__name__)


@dataclass(slots=True)
class BridgeStats:
    """What one execution did, for the tool result and the audit trail."""

    calls: int = 0
    denied: int = 0
    failed: int = 0
    methods: dict[str, int] = field(default_factory=dict)
    #: Worst injection score seen in any payload returned to generated code.
    max_suspicion: float = 0.0
    suspicion_sources: list[str] = field(default_factory=list)


class GatewayBridge:
    """Serves sandbox RPC by delegating to :class:`TelegramGateway`."""

    def __init__(
        self,
        gateway: TelegramGateway,
        *,
        context: CallContext,
        max_calls: int = 200,
    ) -> None:
        self._gateway = gateway
        self._context = CallContext(
            run_id=context.run_id,
            conversation_id=context.conversation_id,
            origin="sandbox",
            interactive=context.interactive,
        )
        self._max_calls = max_calls
        self.stats = BridgeStats()

    async def __call__(self, method: str, arguments: dict[str, Any]) -> Any:
        """The :data:`~tgagent.sandbox.base.RpcHandler` implementation."""
        if self.stats.calls >= self._max_calls:
            self.stats.denied += 1
            raise SandboxError(
                f"This program has made {self.stats.calls} Telegram calls, the configured "
                f"maximum. Narrow the query, or process the data you already have."
            )

        self.stats.calls += 1
        self.stats.methods[method] = self.stats.methods.get(method, 0) + 1

        try:
            result = await self._gateway.call(method, arguments, context=self._context)
        except Exception:
            self.stats.failed += 1
            raise

        self._note_scan(method, result.scan)
        return result.payload

    def _note_scan(self, method: str, scan: ScanResult) -> None:
        if scan.score <= self.stats.max_suspicion:
            return
        self.stats.max_suspicion = scan.score
        if scan.flagged:
            note = f"{method}: {scan.describe()}"
            if note not in self.stats.suspicion_sources:
                self.stats.suspicion_sources.append(note)
            log.warning(
                "sandbox.suspicious_content",
                method=method,
                score=scan.score,
                matches=list(scan.matches),
            )
