"""Command-line interface.

The CLI is a *consumer* of :class:`~tgagent.agent.runtime.AgentRuntime`, not a
part of it: it subscribes to events and renders them, and supplies a confirmation
provider that prompts on the terminal. A web UI or an HTTP API would implement
the same two things and reuse everything else unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from tgagent.__about__ import __version__
from tgagent.agent.events import AgentEvent, EventKind
from tgagent.app import Application
from tgagent.config.settings import Settings, load_settings
from tgagent.errors import TgAgentError
from tgagent.interfaces.telegram_control import TelegramControlBridge
from tgagent.risk import RiskTier
from tgagent.security.confirm import (
    AutoApproveConfirmation,
    CallbackConfirmation,
    ConfirmationOutcome,
    ConfirmationRequest,
)
from tgagent.security.permissions import PermissionEngine
from tgagent.storage.models import ScheduledTask, ScheduleKind
from tgagent.telegram.auth import LoginFlow
from tgagent.telegram.client import TelegramClientManager

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="tgagent",
    help="An autonomous AI agent that operates your Telegram account over MTProto.",
    no_args_is_help=True,
    add_completion=False,
)
tasks_app = typer.Typer(help="Manage scheduled tasks.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect configuration and policy.", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")
app.add_typer(config_app, name="config")

_RISK_STYLE = {
    RiskTier.READ_ONLY: "green",
    RiskTier.REVERSIBLE: "cyan",
    RiskTier.EXTERNALLY_VISIBLE: "yellow",
    RiskTier.DESTRUCTIVE: "red",
    RiskTier.ACCOUNT_SECURITY: "bold red",
}


# --------------------------------------------------------------- plumbing ----
def _run(coro: Any) -> Any:
    """Run a coroutine, turning project errors into clean CLI failures."""
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted.[/yellow]")
        raise typer.Exit(130) from None
    except TgAgentError as exc:
        err_console.print(f"[red]Error:[/red] {exc.user_message}")
        raise typer.Exit(1) from None


async def _ask_on_terminal(request: ConfirmationRequest) -> ConfirmationOutcome:
    """Prompt for confirmation without blocking the event loop."""
    style = _RISK_STYLE.get(request.risk, "yellow")

    def prompt() -> ConfirmationOutcome:
        console.print(
            Panel(
                request.render(),
                title=f"[{style}]Confirmation required[/{style}]",
                border_style=style,
            )
        )
        approved = Confirm.ask("Allow this operation?", default=False)
        if not approved:
            return ConfirmationOutcome(approved=False, reason="Declined at the prompt.")
        remember = False
        if request.risk is not RiskTier.DESTRUCTIVE:
            remember = Confirm.ask(
                f"Allow further {request.method} calls for the rest of this run?",
                default=False,
            )
        return ConfirmationOutcome(
            approved=True, reason="Approved at the prompt.", remember=remember
        )

    return await asyncio.to_thread(prompt)


def _terminal_confirmations(settings: Settings) -> CallbackConfirmation:
    return CallbackConfirmation(_ask_on_terminal, timeout=settings.permissions.confirmation_timeout)


class _Renderer:
    """Turns runtime events into terminal output."""

    def __init__(self, *, verbose: bool, stream: bool) -> None:
        self.verbose = verbose
        self.stream = stream
        self._streaming = False

    def __call__(self, event: AgentEvent) -> None:
        if event.kind is EventKind.TEXT_DELTA and self.stream:
            self._streaming = True
            console.print(event.text, end="", markup=False, highlight=False)
            return

        if self._streaming and event.kind is not EventKind.TEXT_DELTA:
            console.print()
            self._streaming = False

        if event.kind is EventKind.TOOL_CALL_STARTED:
            tool = event.data.get("tool", "?")
            detail = ""
            if self.verbose:
                arguments = json.dumps(event.data.get("arguments", {}), default=str)
                detail = f" {arguments[:160]}"
            console.print(f"[dim]→ {tool}{detail}[/dim]")
        elif event.kind is EventKind.TOOL_CALL_FINISHED:
            ok = event.data.get("ok", True)
            mark, style = ("✓", "dim green") if ok else ("✗", "red")
            console.print(
                f"[{style}]  {mark} {event.data.get('tool')} "
                f"({event.data.get('duration_ms', 0):.0f}ms)[/{style}]"
            )
        elif event.kind is EventKind.CONTEXT_COMPACTED:
            console.print(f"[dim yellow]⟳ {event.text}[/dim yellow]")
        elif event.kind is EventKind.WARNING:
            console.print(f"[yellow]! {event.text}[/yellow]")
        elif event.kind is EventKind.ERROR:
            console.print(f"[red]! {event.text}[/red]")
        elif event.kind is EventKind.ASSISTANT_MESSAGE and not self.stream:
            console.print(Markdown(event.text))


# ------------------------------------------------------------------ auth ----
@app.command()
def login(
    phone: Annotated[str | None, typer.Option(help="Phone number in E.164 format.")] = None,
) -> None:
    """Sign in to Telegram and store an authorised session."""

    async def main() -> None:
        settings = load_settings()
        settings.require_telegram()
        settings.ensure_directories()

        manager = TelegramClientManager(settings.telegram, settings.session_path)
        flow = LoginFlow(
            manager,
            phone=phone or settings.telegram.phone,
            request_phone=lambda: asyncio.to_thread(Prompt.ask, "Phone number (e.g. +15551234567)"),
            request_code=lambda: asyncio.to_thread(Prompt.ask, "Login code from Telegram"),
            request_password=lambda: asyncio.to_thread(
                Prompt.ask, "Two-factor password", password=True
            ),
        )
        try:
            result = await flow.run()
        finally:
            await manager.stop()

        if result.was_already_authorized:
            console.print("[green]Already signed in.[/green]")
        else:
            console.print("[green]Signed in successfully.[/green]")
        console.print(f"  Account : {result.first_name or ''} (id {result.user_id})")
        if result.username:
            console.print(f"  Username: @{result.username}")
        console.print(f"  Session : {result.session_path}")
        console.print(
            "\n[dim]The session file is an authenticated credential. It is "
            "git-ignored; keep it that way.[/dim]"
        )

    _run(main())


@app.command()
def logout() -> None:
    """Revoke the Telegram session and delete the local session file."""

    async def main() -> None:
        settings = load_settings()
        settings.require_telegram()
        manager = TelegramClientManager(settings.telegram, settings.session_path)
        flow = LoginFlow(manager, phone=settings.telegram.phone)
        revoked = await flow.logout()
        console.print(
            "[green]Signed out; the session was revoked on Telegram.[/green]"
            if revoked
            else "[yellow]No active session; local files removed.[/yellow]"
        )

    _run(main())


@app.command()
def whoami() -> None:
    """Show which account is signed in."""

    async def main() -> None:
        application = Application(load_settings())
        try:
            await application.start(connect_telegram=True)
            account = application.account or {}
            console.print(f"[green]Signed in as[/green] {account.get('first_name', '')}".rstrip())
            for key in ("id", "username", "phone", "premium"):
                if key in account:
                    console.print(f"  {key}: {account[key]}")
        finally:
            await application.stop()

    _run(main())


# ------------------------------------------------------------------- run ----
@app.command()
def run(
    prompt: Annotated[str, typer.Argument(help="What you want the agent to do.")],
    conversation: Annotated[
        str | None, typer.Option("--conversation", "-c", help="Continue a conversation by id.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool arguments.")] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Approve every confirmation automatically. Dangerous — the policy "
            "file is then your only protection.",
        ),
    ] = False,
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Block every write operation for this run.")
    ] = False,
) -> None:
    """Run a single request and print the answer."""

    async def main() -> None:
        settings = load_settings()
        if read_only:
            settings.permissions.read_only_mode = True

        confirmations = AutoApproveConfirmation() if yes else _terminal_confirmations(settings)
        if yes:
            console.print("[yellow]--yes: confirmations are auto-approved for this run.[/yellow]")

        application = Application(settings, confirmations=confirmations)
        try:
            await application.start(connect_telegram=True)
            runtime = application.build_runtime()
            renderer = _Renderer(verbose=verbose, stream=settings.llm.stream)
            result = await runtime.run(
                prompt, conversation_id=conversation, interactive=True, on_event=renderer
            )
            _print_result(result, settings.llm.stream)
        finally:
            await application.stop()

    _run(main())


@app.command()
def chat(
    conversation: Annotated[
        str | None, typer.Option("--conversation", "-c", help="Resume a conversation.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Start an interactive session. Ctrl-C cancels a run; Ctrl-D exits."""

    async def main() -> None:
        settings = load_settings()
        application = Application(settings, confirmations=_terminal_confirmations(settings))
        try:
            await application.start(connect_telegram=True)
            runtime = application.build_runtime()
            conversation_id = conversation

            account = application.account or {}
            console.print(
                Panel(
                    f"tgagent {__version__} · account {account.get('username') or account.get('id')}"
                    f" · {len(application.registry)} tools · sandbox: "
                    f"{application.sandbox.name if application.sandbox else 'none'}\n"
                    f"Type your request. Ctrl-C cancels a run, Ctrl-D exits.",
                    title="Interactive session",
                    border_style="cyan",
                )
            )

            while True:
                try:
                    prompt = await asyncio.to_thread(Prompt.ask, "\n[bold cyan]you[/bold cyan]")
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye.[/dim]")
                    return

                if not prompt.strip():
                    continue
                if prompt.strip().lower() in {"/exit", "/quit"}:
                    return

                renderer = _Renderer(verbose=verbose, stream=settings.llm.stream)
                cancel = asyncio.Event()
                task = asyncio.create_task(
                    runtime.run(
                        prompt,
                        conversation_id=conversation_id,
                        interactive=True,
                        on_event=renderer,
                        cancel=cancel,
                    )
                )
                try:
                    result = await task
                except KeyboardInterrupt:
                    cancel.set()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    console.print("[yellow]Cancelled.[/yellow]")
                    continue

                conversation_id = result.conversation_id
                _print_result(result, settings.llm.stream)
        finally:
            await application.stop()

    _run(main())


@app.command()
def listen(
    trigger: Annotated[
        str | None, typer.Option("--trigger", help="Override the trigger word.")
    ] = None,
    scheduler: Annotated[
        bool, typer.Option("--scheduler", help="Also run scheduled tasks.")
    ] = False,
) -> None:
    """Take instructions from your Telegram chats instead of this terminal.

    In any chat, type `agent <instruction>`. The agent gets the instruction plus
    the chat it was typed in, and answers as a reply there. Confirmations are
    asked in the same chat: reply `yes` or `no`.

    Only your own outgoing messages count as commands unless
    `control.allowed_senders` says otherwise. See docs/telegram-control.md.
    """

    async def main() -> None:
        settings = load_settings()
        if trigger:
            settings.control.trigger = trigger

        application = Application(settings)
        # The bridge is built before start() because its confirmation provider has
        # to be the one the gateway captures when Telegram connects.
        bridge = TelegramControlBridge(
            application.telegram,
            application.build_runtime,
            settings.control,
            audit=application.storage.audit,
            confirmation_timeout=settings.permissions.confirmation_timeout,
            log_arguments=settings.logging.log_call_arguments,
        )
        application.use_confirmations(bridge.confirmations)

        try:
            await application.start(connect_telegram=True, start_scheduler=scheduler)
            await bridge.start()
            account = application.account or {}
            console.print(
                Panel(
                    f"Listening as {account.get('username') or account.get('id')}.\n"
                    f"Type [bold]{settings.control.trigger} <instruction>[/bold] in any chat.\n"
                    f"[dim]{settings.control.trigger} stop · "
                    f"{settings.control.trigger} reset · "
                    f"{settings.control.trigger} help[/dim]\n\n"
                    f"Commands are accepted from: "
                    f"{'your own messages' if settings.control.respond_to_self else 'nobody'}"
                    + (
                        f", {', '.join(settings.control.allowed_senders)}"
                        if settings.control.allowed_senders
                        else ""
                    )
                    + "\nPress Ctrl-C to stop.",
                    title="Telegram control",
                    border_style="cyan",
                )
            )
            with contextlib.suppress(KeyboardInterrupt):
                await bridge.wait_closed()
        finally:
            console.print("[dim]Shutting down…[/dim]")
            await bridge.stop()
            await application.stop()

    _run(main())


@app.command()
def serve(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the scheduler in the foreground until interrupted.

    Scheduled runs are unattended: anything needing confirmation is decided by
    `permissions.non_interactive_decision` (deny, by default). If
    `control.enabled` is set, the Telegram control bridge runs alongside it, and
    chat-initiated runs *do* have someone to ask.
    """

    async def main() -> None:
        settings = load_settings()
        application = Application(settings)
        bridge: TelegramControlBridge | None = None
        if settings.control.enabled:
            bridge = TelegramControlBridge(
                application.telegram,
                application.build_runtime,
                settings.control,
                audit=application.storage.audit,
                confirmation_timeout=settings.permissions.confirmation_timeout,
                log_arguments=settings.logging.log_call_arguments,
            )
            application.use_confirmations(bridge.confirmations)

        stop = asyncio.Event()
        try:
            await application.start(connect_telegram=True, start_scheduler=True)
            if bridge is not None:
                await bridge.start()
            enabled = await application.storage.tasks.list_all(enabled_only=True)
            console.print(
                f"[green]Scheduler running[/green] with {len(enabled)} enabled task(s). "
                f"Press Ctrl-C to stop."
            )
            if bridge is not None:
                console.print(
                    f"[green]Telegram control listening[/green] for "
                    f"[bold]{settings.control.trigger} …[/bold] in your chats."
                )
            if verbose:
                _print_tasks(enabled)
            with contextlib.suppress(KeyboardInterrupt):
                await stop.wait()
        finally:
            console.print("[dim]Shutting down…[/dim]")
            if bridge is not None:
                await bridge.stop()
            await application.stop()

    _run(main())


# ----------------------------------------------------------------- tasks ----
@tasks_app.command("list")
def tasks_list() -> None:
    """List scheduled tasks."""

    async def main() -> None:
        application = Application(load_settings())
        try:
            await application.start(connect_telegram=False)
            _print_tasks(await application.storage.tasks.list_all())
        finally:
            await application.stop()

    _run(main())


@tasks_app.command("add")
def tasks_add(
    name: Annotated[str, typer.Argument(help="Unique task name.")],
    prompt: Annotated[str, typer.Argument(help="Instruction to run each time.")],
    cron: Annotated[str | None, typer.Option(help="Cron expression, e.g. '0 8 * * *'.")] = None,
    every: Annotated[int | None, typer.Option(help="Interval in seconds.")] = None,
    once: Annotated[str | None, typer.Option(help="ISO-8601 timestamp for a one-off.")] = None,
    timezone: Annotated[str, typer.Option(help="IANA timezone for cron.")] = "UTC",
) -> None:
    """Create a scheduled task."""
    provided = [x for x in (cron, every, once) if x is not None]
    if len(provided) != 1:
        err_console.print("[red]Provide exactly one of --cron, --every, or --once.[/red]")
        raise typer.Exit(2)

    async def main() -> None:
        from tgagent.scheduler.triggers import next_run_after, validate_schedule

        if cron is not None:
            kind, expression = ScheduleKind.CRON, cron
        elif every is not None:
            kind, expression = ScheduleKind.INTERVAL, str(every)
        else:
            kind, expression = ScheduleKind.ONCE, str(once)

        validate_schedule(kind, expression, timezone)

        application = Application(load_settings())
        try:
            await application.start(connect_telegram=False)
            if await application.storage.tasks.get_by_name(name):
                err_console.print(f"[red]A task named {name!r} already exists.[/red]")
                raise typer.Exit(1)

            now = datetime.now(UTC)
            task = ScheduledTask(
                name=name,
                prompt=prompt,
                kind=kind,
                expression=expression,
                timezone=timezone,
                next_run_at=next_run_after(kind, expression, timezone, now),
                metadata={"created_by": "cli"},
            )
            await application.storage.tasks.create(task)
            console.print(
                f"[green]Created[/green] {name} — next run "
                f"{task.next_run_at.isoformat() if task.next_run_at else 'never'}"
            )
        finally:
            await application.stop()

    _run(main())


@tasks_app.command("remove")
def tasks_remove(name: Annotated[str, typer.Argument()]) -> None:
    """Delete a scheduled task."""

    async def main() -> None:
        application = Application(load_settings())
        try:
            await application.start(connect_telegram=False)
            task = await application.storage.tasks.get_by_name(name)
            if task is None:
                err_console.print(f"[red]No task named {name!r}.[/red]")
                raise typer.Exit(1)
            await application.storage.tasks.delete(task.id)
            console.print(f"[green]Deleted[/green] {name}")
        finally:
            await application.stop()

    _run(main())


@tasks_app.command("run")
def tasks_run(
    name: Annotated[str, typer.Argument()],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run a scheduled task once, right now, as an unattended run."""

    async def main() -> None:
        settings = load_settings()
        application = Application(settings)
        try:
            await application.start(connect_telegram=True)
            task = await application.storage.tasks.get_by_name(name)
            if task is None:
                err_console.print(f"[red]No task named {name!r}.[/red]")
                raise typer.Exit(1)

            console.print(f"[dim]Running {name} (unattended semantics)…[/dim]")
            runtime = application.build_runtime()
            result = await runtime.run(
                task.prompt,
                interactive=False,
                on_event=_Renderer(verbose=verbose, stream=False),
            )
            _print_result(result, streamed=False)
        finally:
            await application.stop()

    _run(main())


# ---------------------------------------------------------------- config ----
@config_app.command("show")
def config_show(
    show_secrets: Annotated[
        bool, typer.Option("--show-secrets", help="Reveal secret values. Be careful.")
    ] = False,
) -> None:
    """Print the effective configuration."""
    settings = load_settings()
    payload = settings.model_dump(mode="json")
    if not show_secrets:
        payload = _mask(payload)
    console.print_json(json.dumps(payload, default=str))


@config_app.command("check")
def config_check() -> None:
    """Validate configuration and report what is and is not ready."""

    async def main() -> None:
        settings = load_settings()
        table = Table(title="Configuration check", show_lines=False)
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")

        ok = "[green]ok[/green]"
        bad = "[red]missing[/red]"
        warn = "[yellow]warning[/yellow]"

        configured = settings.telegram.is_configured()
        table.add_row(
            "Telegram credentials",
            ok if configured else bad,
            "api_id/api_hash present"
            if configured
            else "set TGAGENT_TELEGRAM__API_ID and __API_HASH",
        )
        session_exists = settings.session_path.exists()
        table.add_row(
            "Telegram session",
            ok if session_exists else warn,
            str(settings.session_path) if session_exists else "run `tgagent login`",
        )
        has_key = settings.llm.api_key is not None
        table.add_row(
            "LLM provider",
            ok,
            f"{settings.llm.provider} / {settings.llm.model}"
            + ("" if has_key else " (key from provider environment)"),
        )
        table.add_row("Database", ok, str(settings.storage.database_path))
        table.add_row(
            "Sandbox",
            ok if settings.sandbox.backend != "inprocess" else warn,
            settings.sandbox.backend
            + (" — NO ISOLATION" if settings.sandbox.backend == "inprocess" else ""),
        )
        table.add_row(
            "Policy file",
            ok if settings.permissions.policy_file else "[dim]default[/dim]",
            str(settings.permissions.policy_file or "built-in defaults"),
        )
        console.print(table)

        if not configured:
            raise typer.Exit(1)

    _run(main())


@config_app.command("policy")
def config_policy(
    method: Annotated[
        str | None, typer.Argument(help="Explain the classification of one method.")
    ] = None,
) -> None:
    """Show the permission policy, or explain one method's classification."""
    settings = load_settings()
    from tgagent.config.policy import resolve_permissions

    permissions = resolve_permissions(settings.permissions)

    if method:
        # Asked of the engine rather than recomputed here. An override no longer
        # has to be spelled the way the call is — `send_message` governs
        # `messages.SendMessage` and vice versa — so a lookup reimplemented in
        # this file would confidently report "Override: no" about a policy line
        # that does in fact govern the call. Someone running this command is
        # doing so precisely because they need the answer to be true.
        explanation = PermissionEngine(permissions).explain(method)
        style = _RISK_STYLE.get(explanation.risk, "white")
        lines = [
            f"Method   : {method}",
            f"Risk tier: [{style}]{explanation.risk.value}[/{style}]",
            f"Decision : {explanation.decision.value}",
        ]
        if explanation.from_override:
            governed = ", ".join(explanation.matched_overrides)
            lines.append(f"Override : yes — {governed}")
        else:
            lines.append("Override : no (risk-tier default)")
        console.print(Panel("\n".join(lines), title="Policy explanation"))
        return

    table = Table(title="Permission policy")
    table.add_column("Risk tier")
    table.add_column("Default decision")
    for tier in RiskTier:
        decision = permissions.defaults.get(tier)
        style = _RISK_STYLE.get(tier, "white")
        table.add_row(f"[{style}]{tier.value}[/{style}]", decision.value if decision else "deny")
    console.print(table)

    if permissions.method_overrides:
        overrides = Table(title="Method overrides")
        overrides.add_column("Method")
        overrides.add_column("Decision")
        for name, decision in sorted(permissions.method_overrides.items()):
            overrides.add_row(name, decision.value)
        console.print(overrides)

    console.print(f"read_only_mode: {permissions.read_only_mode}")
    console.print(f"max_outbound_per_run: {permissions.max_outbound_per_run}")
    console.print(f"non_interactive_decision: {permissions.non_interactive_decision.value}")


# ------------------------------------------------------------------ misc ----
@app.command("api")
def api_search(
    query: Annotated[str, typer.Argument(help="What to look for in the Telegram API.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Search the Telegram API index — the same index the agent uses."""
    from tgagent.telegram.schema import TelegramSchemaIndex, format_entry

    settings = load_settings()
    settings.ensure_directories()
    index = TelegramSchemaIndex(settings.schema_cache_path)
    hits = index.search(query, limit=limit)
    if not hits:
        console.print(f"[yellow]No match for {query!r}.[/yellow]")
        return
    console.print(f"[dim]{len(index)} methods indexed[/dim]\n")
    for hit in hits:
        console.print(format_entry(hit.entry))
        console.print()


@app.command()
def audit(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    run_id: Annotated[str | None, typer.Option("--run")] = None,
) -> None:
    """Show recent security-relevant operations."""

    async def main() -> None:
        application = Application(load_settings())
        try:
            await application.start(connect_telegram=False)
            entries = await application.storage.audit.list_recent(run_id=run_id, limit=limit)
            if not entries:
                console.print("[dim]No audit entries yet.[/dim]")
                return
            table = Table(title="Audit log")
            for column in ("When", "Method", "Risk", "Decision", "Target", "Origin", "OK", "Flag"):
                table.add_column(column)
            for entry in entries:
                style = _RISK_STYLE.get(RiskTier(entry.risk), "white") if entry.risk else "white"
                # The injection-scanner score, shown only when there is one. A
                # blank cell here means the content that came back looked clean —
                # which is what almost every row should say.
                flag = f"[yellow]⚠ {entry.suspicion:.2f}[/yellow]" if entry.suspicion else ""
                table.add_row(
                    entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    entry.method,
                    f"[{style}]{entry.risk}[/{style}]",
                    entry.decision,
                    (entry.target or "")[:24],
                    entry.origin,
                    "[green]✓[/green]" if entry.succeeded else "[red]✗[/red]",
                    flag,
                )
            console.print(table)
        finally:
            await application.stop()

    _run(main())


@app.command("sandbox")
def sandbox_check() -> None:
    """Report what the configured sandbox backend actually isolates."""

    async def main() -> None:
        from tgagent.sandbox import create_sandbox
        from tgagent.sandbox.base import ExecutionRequest

        settings = load_settings()
        runner = create_sandbox(settings.sandbox, allow_unsafe=True)
        console.print(Panel(runner.describe_isolation(), title=f"Backend: {runner.name}"))

        async def deny(method: str, _arguments: dict[str, Any]) -> Any:
            raise RuntimeError(f"Telegram is not connected in this check ({method}).")

        probes = {
            "arithmetic": "result = 6 * 7",
            "filesystem (should fail)": "open('/etc/passwd')",
            "os import (should fail)": "import os",
            "network import (should fail)": "import socket",
        }
        table = Table(title="Isolation probes")
        table.add_column("Probe")
        table.add_column("Outcome")
        for label, code in probes.items():
            outcome = await runner.execute(ExecutionRequest(code=code, timeout=20), deny)
            expected_failure = "should fail" in label
            good = outcome.ok is not expected_failure
            mark = "[green]as expected[/green]" if good else "[red]UNEXPECTED[/red]"
            table.add_row(label, f"{mark} — {(outcome.error or 'ok')[:70]}")
        console.print(table)
        await runner.close()

    _run(main())


@app.command()
def version() -> None:
    """Print version information."""
    console.print(f"tgagent {__version__}")
    console.print(f"python  {sys.version.split()[0]}")
    with contextlib.suppress(ImportError):
        import telethon

        console.print(f"telethon {telethon.__version__}")


# --------------------------------------------------------------- helpers ----
def _print_result(result: Any, streamed: bool) -> None:
    if not streamed and result.answer:
        console.print()
    console.print(f"\n[dim]{result.summary_line()}[/dim]")
    if result.errors:
        for message in result.errors[:5]:
            console.print(f"[yellow]  ! {message}[/yellow]")
    console.print(f"[dim]conversation: {result.conversation_id}[/dim]")


def _print_tasks(tasks: list[ScheduledTask]) -> None:
    from tgagent.scheduler.triggers import describe_schedule

    if not tasks:
        console.print("[dim]No scheduled tasks.[/dim]")
        return
    table = Table(title="Scheduled tasks")
    for column in ("Name", "Schedule", "Enabled", "Next run", "Last status", "Runs"):
        table.add_column(column)
    for task in tasks:
        table.add_row(
            task.name,
            describe_schedule(task),
            "[green]yes[/green]" if task.enabled else "[dim]no[/dim]",
            task.next_run_at.strftime("%Y-%m-%d %H:%M") if task.next_run_at else "-",
            task.last_status.value if task.last_status else "-",
            str(task.run_count),
        )
    console.print(table)


def _mask(payload: Any) -> Any:
    """Replace anything that looks secret before printing configuration."""
    hints = ("api_hash", "api_key", "password", "secret", "token", "proxy", "phone")
    if isinstance(payload, dict):
        return {
            key: ("***" if any(h in key.lower() for h in hints) and value else _mask(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_mask(item) for item in payload]
    return payload


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
