"""The composition root.

Every dependency in the system is constructed here and nowhere else. Modules
receive what they need through their constructors, which is what keeps the graph
acyclic, the interfaces swappable, and the tests able to substitute doubles
without patching globals.

:class:`Application` also owns lifecycle: connect in a deterministic order, and
shut down in the reverse one so nothing is torn out from under an in-flight
operation.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

from tgagent.agent.events import RunResult
from tgagent.agent.runtime import AgentRuntime, RuntimeDependencies
from tgagent.config.policy import resolve_permissions
from tgagent.config.settings import Settings, load_settings
from tgagent.errors import ConfigError, TgAgentError
from tgagent.llm.base import LLMProvider
from tgagent.llm.registry import create_provider
from tgagent.observability.logging import configure_logging, get_logger
from tgagent.observability.redaction import secret_registry
from tgagent.sandbox import create_sandbox
from tgagent.sandbox.base import SandboxRunner
from tgagent.scheduler.scheduler import Scheduler
from tgagent.security.confirm import AutoDenyConfirmation, ConfirmationProvider
from tgagent.security.permissions import PermissionEngine, granted
from tgagent.storage.models import ScheduledTask
from tgagent.storage.sqlite import SQLiteStorage
from tgagent.telegram.client import TelegramClientManager
from tgagent.telegram.gateway import TelegramGateway
from tgagent.telegram.history import HistoryReader
from tgagent.telegram.media import MediaManager
from tgagent.telegram.schema import TelegramSchemaIndex
from tgagent.telegram.serialize import entity_to_dict
from tgagent.tools import build_default_registry
from tgagent.tools.base import ToolRegistry

log = get_logger(__name__)


class Application:
    """Wires the system together and owns its lifecycle."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        confirmations: ConfirmationProvider | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        # Policy is resolved once, at construction: a run must not be able to
        # observe the policy changing halfway through. The data directory is passed
        # so that overrides written from a chat are picked up too; changing one
        # while the process runs goes through RuntimeAdmin, which refuses while any
        # run is in flight for exactly the reason above.
        self.settings.permissions = resolve_permissions(
            self.settings.permissions, data_dir=self.settings.data_dir
        )
        self.settings.ensure_directories()

        configure_logging(self.settings.logging)
        self._register_secrets()

        self.confirmations: ConfirmationProvider = confirmations or AutoDenyConfirmation()
        self.permissions = PermissionEngine(self.settings.permissions)

        self.storage = SQLiteStorage(
            self.settings.storage.database_path,  # type: ignore[arg-type]
            busy_timeout_ms=self.settings.storage.busy_timeout_ms,
        )
        self.schema = TelegramSchemaIndex(self.settings.schema_cache_path)

        self._provider: LLMProvider | None = provider
        self._telegram: TelegramClientManager | None = None
        self.gateway: TelegramGateway | None = None
        self.history: HistoryReader | None = None
        self.media: MediaManager | None = None
        self.sandbox: SandboxRunner | None = None
        self.scheduler: Scheduler | None = None
        self.registry: ToolRegistry = build_default_registry(self.settings)
        self.account: dict[str, Any] | None = None

        self._started = False
        self._telegram_connected = False

    # ------------------------------------------------------------ lifecycle --
    async def start(
        self,
        *,
        connect_telegram: bool = True,
        start_scheduler: bool = False,
        allow_unsafe_sandbox: bool = False,
    ) -> Self:
        """Bring the application up.

        Telegram is optional so that ``config``, ``policy``, and offline tests
        work without credentials; the tool set adapts to what is available.
        """
        if self._started:
            return self

        await self.storage.connect()
        await self._prune_audit()

        self.sandbox = create_sandbox(self.settings.sandbox, allow_unsafe=allow_unsafe_sandbox)

        if connect_telegram:
            await self._connect_telegram()

        if start_scheduler and self.settings.features.scheduling:
            self.scheduler = Scheduler(
                self.storage.tasks, self._run_scheduled_task, self.settings.scheduler
            )
            await self.scheduler.start()

        self._started = True
        log.info(
            "app.started",
            telegram=self._telegram_connected,
            sandbox=self.sandbox.name,
            tools=len(self.registry),
            provider=self.settings.llm.provider,
        )
        return self

    async def stop(self) -> None:
        """Shut down in reverse dependency order."""
        if self.scheduler is not None:
            await self.scheduler.stop()
            self.scheduler = None

        if self.sandbox is not None:
            await self.sandbox.close()
            self.sandbox = None

        if self._telegram is not None:
            await self._telegram.stop()
            self._telegram = None
            self._telegram_connected = False

        if self._provider is not None:
            with contextlib.suppress(Exception):
                await self._provider.aclose()
            self._provider = None

        await self.storage.close()
        self._started = False
        log.info("app.stopped")

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    def use_confirmations(self, provider: ConfirmationProvider) -> None:
        """Choose who answers CONFIRM decisions.

        Only valid before :meth:`start`. The gateway captures the provider when
        Telegram connects, so a later swap would look like it worked and quietly
        leave the old provider in charge — which for a security control is the
        worst kind of no-op. Interfaces whose provider needs the running loop (the
        Telegram control bridge answers in a chat, and so must exist before the
        gateway does) construct it and hand it over here.
        """
        if self._started:
            raise ConfigError("Confirmations must be chosen before the application starts.")
        self.confirmations = provider

    # -------------------------------------------------------------- access ---
    @property
    def provider(self) -> LLMProvider:
        """The LLM provider, constructed on first use."""
        if self._provider is None:
            try:
                self._provider = create_provider(self.settings.llm)
            except TgAgentError:
                raise
            except Exception as exc:
                raise ConfigError(f"Could not create the LLM provider: {exc}") from exc
            if key := self.settings.llm.api_key:
                secret_registry.register(key.get_secret_value())
        return self._provider

    def reload_llm(self) -> None:
        """Drop the cached provider so the next run builds one from settings.

        What makes ``agent llm model …`` real rather than cosmetic: the provider is
        constructed once and cached, so without this the process would report the
        new model and go on using the old one — a lie, and a confusing one.
        """
        provider, self._provider = self._provider, None
        if provider is None:
            return
        # Closed in the background: this is called from a chat handler, and the
        # old client's shutdown is not something the operator should wait for.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._close_provider(provider))
        log.info("app.llm_reloaded", model=self.settings.llm.model)

    @staticmethod
    async def _close_provider(provider: LLMProvider) -> None:
        with contextlib.suppress(Exception):
            await provider.aclose()

    @property
    def telegram(self) -> TelegramClientManager:
        if self._telegram is None:
            self.settings.require_telegram()
            self._telegram = TelegramClientManager(
                self.settings.telegram, self.settings.session_path
            )
        return self._telegram

    @property
    def telegram_connected(self) -> bool:
        return self._telegram_connected

    def build_runtime(self) -> AgentRuntime:
        """Construct an :class:`AgentRuntime` bound to the live subsystems."""
        return AgentRuntime(
            self.provider,
            self.registry,
            self.settings,
            RuntimeDependencies(
                gateway=self.gateway,
                history=self.history,
                media=self.media,
                schema=self.schema,
                sandbox=self.sandbox,
                memory=self.storage.memory,
                tasks=self.storage.tasks,
                watches=self.storage.watches,
                conversations=self.storage.conversations,
                permissions=self.permissions,
                confirmations=self.confirmations,
                scheduler_running=lambda: self.scheduler is not None and self.scheduler.running,
                account=self.account,
            ),
        )

    # ------------------------------------------------------------ internals --
    async def _connect_telegram(self) -> None:
        manager = self.telegram
        await manager.start(require_authorization=True)
        self._telegram_connected = True

        self.gateway = TelegramGateway(
            manager,
            permissions=self.permissions,
            confirmations=self.confirmations,
            audit=self.storage.audit,
            permission_settings=self.settings.permissions,
            logging_settings=self.settings.logging,
            features=self.settings.features,
        )
        self.history = HistoryReader(self.gateway)
        self.media = MediaManager(self.gateway, self.settings.media)

        if manager.me is not None:
            self.account = entity_to_dict(manager.me)

        # Building the API index takes under a second and is cached, but doing
        # it off the event loop keeps start-up snappy.
        await asyncio.to_thread(self.schema.ensure_loaded)

        if self.settings.media.retention_days > 0:
            await asyncio.to_thread(self.media.cleanup)

    def _register_secrets(self) -> None:
        """Teach the log redactor the literal secrets this process holds."""
        telegram = self.settings.telegram
        secret_registry.register(
            telegram.api_hash.get_secret_value(),
            telegram.proxy.get_secret_value() if telegram.proxy else None,
            telegram.phone,
        )
        if key := self.settings.llm.api_key:
            secret_registry.register(key.get_secret_value())

    async def _prune_audit(self) -> None:
        days = self.settings.storage.audit_retention_days
        if days <= 0:
            return
        with contextlib.suppress(Exception):
            removed = await self.storage.audit.prune(datetime.now(UTC) - timedelta(days=days))
            if removed:
                log.info("app.audit_pruned", removed=removed, retention_days=days)

    async def run_task(self, task: ScheduledTask, *, on_event: Any = None) -> RunResult:
        """Execute *task* as an unattended agent run.

        ``interactive=False`` is the important part: no human can answer a
        confirmation, so the permission engine falls back to the configured
        non-interactive decision rather than blocking forever.

        A task may also carry grants — operations its owner approved when the task
        was created, recorded on the row. They are applied around *this run only*,
        so a chat run happening at the same time is unaffected; see
        :func:`~tgagent.security.permissions.granted`.

        Public, and the only definition of what running a task means. ``tgagent
        tasks run`` reaching for its own runtime is how a task comes to behave one
        way when tested and another at 04:00 — which, with grants on the row, it
        did: the same task worked on a schedule and was refused from the CLI.
        """
        runtime = self.build_runtime()
        grants = [str(method) for method in (task.metadata.get("grants") or [])]
        if grants:
            log.info("app.task_grants", task=task.name, methods=grants)
        with granted(grants, source=f"scheduled task {task.name!r}"):
            return await runtime.run(
                task.prompt,
                conversation_id=task.metadata.get("conversation_id"),
                interactive=False,
                on_event=on_event,
            )

    async def _run_scheduled_task(self, task: ScheduledTask) -> str:
        """The scheduler's callback: run the task, report its answer."""
        result = await self.run_task(task)
        if result.errors:
            log.warning("app.scheduled_task_errors", task=task.name, errors=result.errors)
        return result.answer


async def create_application(
    settings: Settings | None = None,
    *,
    confirmations: ConfirmationProvider | None = None,
    connect_telegram: bool = True,
    start_scheduler: bool = False,
) -> Application:
    """Convenience factory: construct and start in one call."""
    app = Application(settings, confirmations=confirmations)
    await app.start(connect_telegram=connect_telegram, start_scheduler=start_scheduler)
    return app
