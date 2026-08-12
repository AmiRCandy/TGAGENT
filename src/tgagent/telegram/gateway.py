"""The Telegram gateway — the single choke point for every API call.

Nothing reaches MTProto except through :meth:`TelegramGateway.call`. Curated
tools go through it; model-generated code in the sandbox goes through it over
RPC; the scheduler goes through it. That is the property the whole security
model rests on: there is exactly one place where classification, authorisation,
confirmation, rate limiting, auditing, and output sanitisation happen, so none
of them can be bypassed by choosing a different route into the API.

Call shapes
-----------
Two, dispatched on the method name:

* **Friendly** — ``send_message``, ``get_messages``, … resolved on the Telethon
  client and called with keyword arguments.
* **Raw TL** — ``messages.Search``, ``channels.GetParticipants``, … the request
  class is located, its arguments coerced from JSON, and the object invoked.
  This is what makes the full ~824-method surface reachable.

Every result is serialised through :mod:`tgagent.telegram.serialize` before it
leaves, so no live Telethon object — and no ``bytes`` blob or credential — ever
escapes into the sandbox or the model's context.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tgagent.config.settings import FeatureFlags, LoggingSettings, PermissionSettings
from tgagent.errors import (
    OperationCancelled,
    PermissionDenied,
    TelegramCallError,
    TelegramError,
)
from tgagent.observability.logging import get_logger
from tgagent.risk import PolicyDecision, RiskTier
from tgagent.security.confirm import (
    ConfirmationProvider,
    ConfirmationRequest,
)
from tgagent.security.injection import ScanResult, scan_many
from tgagent.security.permissions import (
    AuthorizationResult,
    OperationRequest,
    PermissionEngine,
)
from tgagent.storage.base import AuditRepository
from tgagent.storage.models import AuditEntry
from tgagent.telegram.client import TelegramClientManager
from tgagent.telegram.entities import (
    PEER_ARGUMENT_NAMES,
    EntityResolver,
    coerce_argument,
    extract_target,
)
from tgagent.telegram.serialize import (
    extract_text_fields,
    to_jsonable,
)

log = get_logger(__name__)

#: Annotation stand-in used when a **kwargs parameter has no declared type
#: but its name says it is a peer, so string references still get resolved.
_PEER_HINT = "TypeInputPeer"


@dataclass(slots=True)
class GatewayResult:
    """The outcome of one authorised, executed Telegram call."""

    method: str
    payload: Any
    risk: RiskTier
    decision: PolicyDecision
    duration_ms: float
    #: Injection scan over any free text in the payload.
    scan: ScanResult = field(default_factory=lambda: ScanResult(0.0, ()))
    target: str | None = None


@dataclass(slots=True)
class CallContext:
    """Per-run context threaded through every gateway call."""

    run_id: str = ""
    conversation_id: str | None = None
    #: ``tool`` | ``sandbox`` | ``scheduler``.
    origin: str = "tool"
    #: Whether a human is available to answer confirmation prompts.
    interactive: bool = True


class TelegramGateway:
    """Authorises, executes, audits, and sanitises Telegram operations."""

    def __init__(
        self,
        manager: TelegramClientManager,
        *,
        permissions: PermissionEngine,
        confirmations: ConfirmationProvider,
        audit: AuditRepository | None = None,
        permission_settings: PermissionSettings | None = None,
        logging_settings: LoggingSettings | None = None,
        features: FeatureFlags | None = None,
        scan_results: bool = True,
    ) -> None:
        self._manager = manager
        self._permissions = permissions
        self._confirmations = confirmations
        self._audit = audit
        self._settings = permission_settings or permissions.settings
        self._logging = logging_settings or LoggingSettings()
        self._features = features or FeatureFlags()
        self._scan_results = scan_results

        self._resolver: EntityResolver | None = None
        self._last_write_at = 0.0
        self._write_gate = asyncio.Lock()

    # --------------------------------------------------------------- public --
    @property
    def resolver(self) -> EntityResolver:
        if self._resolver is None:
            self._resolver = EntityResolver(self._manager.client)
        return self._resolver

    async def call(
        self,
        method: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: CallContext | None = None,
        projector: Callable[[Any], Any] | None = None,
    ) -> GatewayResult:
        """Authorise and execute one Telegram operation.

        ``projector`` runs on the raw Telethon result *inside* the gateway,
        before serialisation. It exists so curated tools can emit compact,
        token-cheap projections without anyone having to obtain a live
        Telethon object outside this class.

        Raises :class:`~tgagent.errors.PermissionDenied` if policy refuses, and
        :class:`~tgagent.errors.TelegramCallError` if Telegram does.
        """
        arguments = dict(arguments or {})
        ctx = context or CallContext()
        started = time.perf_counter()

        request = OperationRequest(
            method=method,
            arguments=arguments,
            target=extract_target(arguments),
            origin=ctx.origin,
        )

        verdict = self._permissions.authorize(request, interactive=ctx.interactive)

        if verdict.needs_confirmation:
            verdict = await self._ask(request, verdict)

        if verdict.decision is PolicyDecision.DENY:
            await self._record(
                request,
                verdict,
                ctx,
                succeeded=False,
                error=verdict.reason,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            log.warning(
                "gateway.denied", method=method, risk=verdict.risk.value, reason=verdict.reason
            )
            raise PermissionDenied(
                f"{method} was not permitted: {verdict.reason}",
                method=method,
                risk=verdict.risk.value,
            )

        if verdict.risk.at_least(RiskTier.EXTERNALLY_VISIBLE):
            await self._throttle_writes()

        try:
            raw = await self._execute(method, arguments)
        except asyncio.CancelledError:
            raise
        except PermissionDenied:
            raise
        except Exception as exc:
            error = self._translate(exc, method)
            await self._record(
                request,
                verdict,
                ctx,
                succeeded=False,
                error=str(error),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise error from exc

        self._permissions.note_outbound(verdict.risk)
        duration_ms = (time.perf_counter() - started) * 1000

        if projector is not None:
            try:
                raw = projector(raw)
            except Exception as exc:  # noqa: BLE001 - fall back to the generic form
                log.warning("gateway.projector_failed", method=method, error=str(exc))
        payload = to_jsonable(raw)
        scan_result = self._scan(payload)

        await self._record(
            request,
            verdict,
            ctx,
            succeeded=True,
            error=None,
            duration_ms=duration_ms,
            suspicion=scan_result.score,
        )
        log.info(
            "gateway.call",
            method=method,
            risk=verdict.risk.value,
            decision=verdict.decision.value,
            origin=ctx.origin,
            duration_ms=round(duration_ms, 1),
            suspicion=scan_result.score or None,
        )

        return GatewayResult(
            method=method,
            payload=payload,
            risk=verdict.risk,
            decision=verdict.decision,
            duration_ms=duration_ms,
            scan=scan_result,
            target=request.target,
        )

    async def download_media(
        self,
        peer: str | int,
        message_id: int,
        destination: str,
        *,
        context: CallContext | None = None,
    ) -> str | None:
        """Download one message's media to *destination*.

        A dedicated primitive because ``client.download_media`` needs the live
        ``Message`` object, which cannot survive JSON serialisation and so
        cannot travel through the generic :meth:`call` path. It is authorised
        and audited exactly like any other operation — the choke point holds.
        """
        ctx = context or CallContext()
        started = time.perf_counter()
        request = OperationRequest(
            method="download_media",
            arguments={"peer": str(peer), "message_id": message_id, "file": destination},
            target=str(peer),
            origin=ctx.origin,
        )

        verdict = self._permissions.authorize(request, interactive=ctx.interactive)
        if verdict.needs_confirmation:
            verdict = await self._ask(request, verdict)
        if verdict.decision is PolicyDecision.DENY:
            await self._record(
                request,
                verdict,
                ctx,
                succeeded=False,
                error=verdict.reason,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise PermissionDenied(
                f"download_media was not permitted: {verdict.reason}",
                method="download_media",
                risk=verdict.risk.value,
            )

        try:
            await self._manager.ensure_connected()
            client = self._manager.client
            entity = await self.resolver.input_entity(peer)
            messages = await client.get_messages(entity, ids=[int(message_id)])
            message = messages[0] if messages else None
            if message is None or getattr(message, "media", None) is None:
                raise TelegramError(f"Message {message_id} has no media to download.")
            written = await client.download_media(message, file=destination)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self._translate(exc, "download_media")
            await self._record(
                request,
                verdict,
                ctx,
                succeeded=False,
                error=str(error),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise error from exc

        duration_ms = (time.perf_counter() - started) * 1000
        await self._record(
            request, verdict, ctx, succeeded=True, error=None, duration_ms=duration_ms
        )
        log.info("gateway.download", message_id=message_id, duration_ms=round(duration_ms, 1))
        return str(written) if written else None

    async def call_raw_object(self, request_obj: Any, *, context: CallContext | None = None) -> Any:
        """Invoke an already-constructed TL request object.

        Used internally by helpers that build requests in Python; the method
        name is derived from the class so policy still applies.
        """
        name = type(request_obj).__name__.removesuffix("Request")
        module = type(request_obj).__module__.rsplit(".", 1)[-1]
        method = f"{module}.{name}" if module not in ("functions", "tl") else name
        result = await self.call(method, _tl_object_arguments(request_obj), context=context)
        return result.payload

    # -------------------------------------------------------------- execute --
    async def _execute(self, method: str, arguments: dict[str, Any]) -> Any:
        await self._manager.ensure_connected()
        client = self._manager.client

        if "." in method or method[:1].isupper():
            return await self._execute_raw(client, method, arguments)
        return await self._execute_friendly(client, method, arguments)

    async def _execute_friendly(self, client: Any, method: str, arguments: dict[str, Any]) -> Any:
        member = getattr(client, method, None)
        if member is None or not callable(member):
            raise TelegramError(
                f"Telethon has no client method {method!r}. Use telegram_api_search "
                f"to find the right name."
            )

        prepared = await self._prepare_friendly_arguments(method, member, arguments)
        result = member(**prepared)

        if inspect.isawaitable(result):
            return await result
        # Several friendly methods return async generators (iter_messages, …).
        if hasattr(result, "__aiter__"):
            limit = int(arguments.get("limit") or 100)
            collected: list[Any] = []
            async for item in result:
                collected.append(item)
                if len(collected) >= limit:
                    break
            return collected
        return result

    async def _prepare_friendly_arguments(
        self, method: str, member: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            signature = inspect.signature(member)
        except (TypeError, ValueError):
            return arguments

        # Several Telethon methods take **kwargs and forward them; rejecting
        # names the signature does not list would make those uncallable.
        accepts_extra = any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values())

        prepared: dict[str, Any] = {}
        for name, value in arguments.items():
            parameter = signature.parameters.get(name)
            if parameter is None:
                if accepts_extra:
                    prepared[name] = await coerce_argument(
                        value, _PEER_HINT if name in PEER_ARGUMENT_NAMES else "", self.resolver
                    )
                    continue
                valid = ", ".join(p for p in signature.parameters if p != "self")
                raise TelegramError(
                    f"{method}() has no parameter {name!r}. Valid parameters: {valid}."
                )
            annotation = parameter.annotation
            rendered = annotation if isinstance(annotation, str) else str(annotation)
            prepared[name] = await coerce_argument(value, rendered, self.resolver)
        return prepared

    async def _execute_raw(self, client: Any, method: str, arguments: dict[str, Any]) -> Any:
        request_cls = _resolve_request_class(method)
        try:
            signature = inspect.signature(request_cls.__init__)
        except (TypeError, ValueError) as exc:  # pragma: no cover
            raise TelegramError(f"Cannot introspect {method}: {exc}") from exc

        kwargs: dict[str, Any] = {}
        for name, value in arguments.items():
            parameter = signature.parameters.get(name)
            if parameter is None:
                valid = ", ".join(p for p in signature.parameters if p != "self")
                raise TelegramError(
                    f"{method} has no parameter {name!r}. Valid parameters: {valid}."
                )
            annotation = parameter.annotation
            rendered = annotation if isinstance(annotation, str) else str(annotation)
            kwargs[name] = await coerce_argument(value, rendered, self.resolver)

        missing = [
            name
            for name, parameter in signature.parameters.items()
            if name not in ("self",)
            and parameter.default is inspect.Parameter.empty
            and name not in kwargs
            and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        ]
        if missing:
            raise TelegramError(f"{method} is missing required parameter(s): {', '.join(missing)}.")

        return await client(request_cls(**kwargs))

    # ------------------------------------------------------------ policy ----
    async def _ask(
        self, request: OperationRequest, verdict: AuthorizationResult
    ) -> AuthorizationResult:
        target_display = request.target
        # Show a human-meaningful name where we can resolve one cheaply.
        if target_display and self._manager.connected:
            try:
                described = await self.resolver.describe(target_display)
                target_display = f"{described.display} (id {described.id})"
            except TelegramError:
                pass

        outcome = await self._confirmations.confirm(
            ConfirmationRequest(
                method=request.method,
                risk=verdict.risk,
                summary=verdict.prompt,
                target=target_display,
                arguments=request.arguments,
                reason=verdict.reason,
            )
        )
        if outcome.approved:
            return AuthorizationResult(
                PolicyDecision.ALLOW,
                verdict.risk,
                outcome.reason or "Approved by the user.",
            )
        return AuthorizationResult(
            PolicyDecision.DENY,
            verdict.risk,
            outcome.reason or "The user declined the confirmation prompt.",
        )

    async def _throttle_writes(self) -> None:
        """Space out externally-visible operations.

        Telegram's anti-spam heuristics act on accounts, not applications; an
        agent that loops can get a real person limited. The floor is cheap
        insurance.
        """
        minimum = self._settings.min_seconds_between_writes
        if minimum <= 0:
            return
        async with self._write_gate:
            elapsed = time.monotonic() - self._last_write_at
            if elapsed < minimum:
                await asyncio.sleep(minimum - elapsed)
            self._last_write_at = time.monotonic()

    # -------------------------------------------------------------- output --
    def _scan(self, payload: Any) -> ScanResult:
        if not (self._scan_results and self._features.injection_scanner):
            return ScanResult(0.0, ())
        texts = extract_text_fields(payload)
        return scan_many(texts) if texts else ScanResult(0.0, ())

    # --------------------------------------------------------------- audit --
    async def _record(
        self,
        request: OperationRequest,
        verdict: AuthorizationResult,
        ctx: CallContext,
        *,
        succeeded: bool,
        error: str | None,
        duration_ms: float,
        suspicion: float = 0.0,
    ) -> None:
        if self._audit is None:
            return
        preview: str | None = None
        if self._logging.log_call_arguments:
            preview = str(request.arguments)[:500]
        entry = AuditEntry(
            run_id=ctx.run_id,
            conversation_id=ctx.conversation_id,
            method=request.method,
            risk=verdict.risk.value,
            decision=verdict.decision.value,
            target=request.target,
            argument_digest=request.argument_digest,
            argument_preview=preview,
            succeeded=succeeded,
            error=error,
            duration_ms=duration_ms,
            suspicion=suspicion,
            origin=ctx.origin,
        )
        if suspicion:
            # A score describes what came back, not a failure, so it has its own
            # column and its own structured event. It deliberately does not touch
            # ``entry.error``: that field is the reason a call did not work, and
            # a call that scored above zero may have worked perfectly.
            log.warning(
                "gateway.content_flagged",
                method=request.method,
                target=request.target,
                run_id=ctx.run_id,
                suspicion=round(suspicion, 2),
            )
        try:
            await self._audit.record(entry)
        except Exception as exc:  # noqa: BLE001 - auditing must never break a run
            log.error("gateway.audit_failed", method=request.method, error=str(exc))

    # --------------------------------------------------------------- errors --
    @staticmethod
    def _translate(exc: Exception, method: str) -> Exception:
        """Map Telethon exceptions onto the project's taxonomy."""
        from telethon import errors

        if isinstance(exc, TelegramError):
            return exc
        if isinstance(exc, asyncio.CancelledError):  # pragma: no cover
            return OperationCancelled("The Telegram call was cancelled.")

        if isinstance(exc, errors.FloodWaitError):
            return TelegramCallError(
                f"Telegram rate-limited {method}; it asks to wait {exc.seconds}s. "
                f"Retry after that delay or reduce request volume.",
                method=method,
                retryable=True,
                retry_after=float(exc.seconds),
            )
        if isinstance(exc, (errors.ChatWriteForbiddenError, errors.ChatAdminRequiredError)):
            return TelegramCallError(
                f"{method} was refused: the account lacks permission in that chat ({exc}).",
                method=method,
            )
        if isinstance(exc, errors.UserPrivacyRestrictedError):
            return TelegramCallError(
                f"{method} was refused by that user's privacy settings.", method=method
            )
        if isinstance(exc, errors.AuthKeyError):
            return TelegramCallError(
                f"The Telegram session is no longer valid; sign in again. ({exc})",
                method=method,
            )
        if isinstance(exc, (errors.ServerError, errors.TimedOutError)):
            return TelegramCallError(
                f"Telegram had a server-side problem handling {method}: {exc}",
                method=method,
                retryable=True,
            )
        if isinstance(exc, errors.RPCError):
            return TelegramCallError(f"Telegram rejected {method}: {exc}", method=method)
        if isinstance(exc, (TypeError, ValueError)):
            return TelegramCallError(
                f"{method} was called with invalid arguments: {exc}", method=method
            )
        return TelegramCallError(f"{method} failed: {exc}", method=method)


# ------------------------------------------------------------------ helpers --
def _resolve_request_class(path: str) -> Any:
    """Locate a TL request class from a dotted path like ``messages.Search``."""
    from telethon.tl import functions
    from telethon.tl.tlobject import TLRequest

    cleaned = path.strip()
    namespace, _, name = cleaned.rpartition(".")
    if not name.endswith("Request"):
        name = f"{name}Request"

    module: Any = functions
    if namespace:
        module = getattr(functions, namespace, None)
        if module is None:
            available = ", ".join(sorted(_function_namespaces()))
            raise TelegramError(
                f"Unknown Telegram API namespace {namespace!r}. Available: {available}."
            )

    request_cls = getattr(module, name, None)
    if request_cls is None or not (
        isinstance(request_cls, type) and issubclass(request_cls, TLRequest)
    ):
        raise TelegramError(
            f"Unknown Telegram API method {path!r}. Use telegram_api_search to find "
            f"the correct name."
        )
    return request_cls


def _function_namespaces() -> list[str]:
    import pkgutil

    from telethon.tl import functions

    return [m.name for m in pkgutil.iter_modules(functions.__path__)]


def _tl_object_arguments(obj: Any) -> dict[str, Any]:
    """Shallow argument view of a constructed TL request, for the audit trail."""
    out: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_") or name.isupper():
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001, S112 - best-effort audit view of a TL object
            continue
        if not callable(value):
            out[name] = value
    return out
