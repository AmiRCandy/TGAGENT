"""The agent loop.

Not ``user → LLM → response``. One run is a bounded, observable, cancellable
sequence of steps, each of which may call several tools:

    load history → compact if needed → ask the model → execute tool calls
    → feed results back → repeat until the model stops calling tools

Everything that could run away is bounded: steps, tool calls, wall clock per
step, wall clock per run, and consecutive tool failures. Everything that could
surprise the user is observable: each phase emits an event, and every Telegram
call lands in the audit log.

The runtime knows nothing about any interface. It takes an event callback and
returns a result; the CLI, a web UI, and the scheduler all drive it the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tgagent.agent.context import ContextManager
from tgagent.agent.events import AgentEvent, EventKind, RunResult
from tgagent.agent.prompts import COMPACTION_PROMPT, build_system_prompt
from tgagent.config.settings import Settings
from tgagent.errors import (
    LLMConfigError,
    LLMError,
    OperationCancelled,
    PermissionDenied,
    TgAgentError,
    ToolNotFound,
)
from tgagent.llm.base import (
    Completion,
    ContentPart,
    GenerationParams,
    LLMProvider,
    Message,
    StopReason,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from tgagent.observability.logging import bind_run_context, clear_run_context, get_logger
from tgagent.risk import TrustLevel
from tgagent.security.trust import UntrustedContent, wrap_untrusted
from tgagent.storage.base import ConversationRepository
from tgagent.storage.models import Conversation, MessageRole, StoredMessage
from tgagent.tools.base import ToolContext, ToolRegistry, ToolResult

log = get_logger(__name__)

EventCallback = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class RuntimeDependencies:
    """Optional subsystems a run may use.

    Bundled so the composition root wires once and the runtime signature stays
    readable. All are optional: the agent still runs (with fewer tools) when
    Telegram is not connected, which is what makes offline testing possible.
    """

    gateway: Any = None
    history: Any = None
    media: Any = None
    schema: Any = None
    sandbox: Any = None
    memory: Any = None
    tasks: Any = None
    watches: Any = None
    conversations: ConversationRepository | None = None
    permissions: Any = None
    #: Only used by tools that must get the owner's decision *now* for work that
    #: will run later; per-call authorisation stays inside the gateway.
    confirmations: Any = None
    #: Callable, because the scheduler starts after the runtime is wired.
    scheduler_running: Any = None
    account: dict[str, Any] | None = None


class AgentRuntime:
    """Executes agent runs."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        settings: Settings,
        dependencies: RuntimeDependencies | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._settings = settings
        self._deps = dependencies or RuntimeDependencies()
        self._context = ContextManager(
            provider, settings.agent, compaction_prompt=COMPACTION_PROMPT
        )

    # ---------------------------------------------------------------- run ----
    async def run(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        interactive: bool = True,
        on_event: EventCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Execute one request end to end."""
        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        cancel = cancel or asyncio.Event()
        emit = _make_emitter(on_event)

        bind_run_context(run_id=run_id, conversation_id=conversation_id)
        try:
            async with asyncio.timeout(self._settings.agent.run_timeout):
                return await self._run_inner(
                    prompt,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    interactive=interactive,
                    emit=emit,
                    cancel=cancel,
                    started=started,
                )
        except TimeoutError:
            await emit(AgentEvent.make(EventKind.ERROR, "The run exceeded its overall time limit."))
            return RunResult(
                run_id=run_id,
                conversation_id=conversation_id or "",
                answer=(
                    f"I ran out of time (limit: {self._settings.agent.run_timeout:.0f}s) "
                    f"before finishing. Try narrowing the request."
                ),
                duration_ms=(time.perf_counter() - started) * 1000,
                stopped_because="run_timeout",
            )
        except asyncio.CancelledError:
            await emit(AgentEvent.make(EventKind.RUN_FINISHED, "Cancelled."))
            return RunResult(
                run_id=run_id,
                conversation_id=conversation_id or "",
                answer="Cancelled.",
                duration_ms=(time.perf_counter() - started) * 1000,
                cancelled=True,
                stopped_because="cancelled",
            )
        finally:
            clear_run_context()

    async def _run_inner(
        self,
        prompt: str,
        *,
        run_id: str,
        conversation_id: str | None,
        interactive: bool,
        emit: Callable[[AgentEvent], Awaitable[None]],
        cancel: asyncio.Event,
        started: float,
    ) -> RunResult:
        settings = self._settings

        if self._deps.permissions is not None:
            self._deps.permissions.reset_run_counters()

        conversation = await self._load_or_create_conversation(conversation_id, prompt)
        history = await self._load_history(conversation.id)

        tool_context = ToolContext(
            run_id=run_id,
            settings=settings,
            conversation_id=conversation.id,
            interactive=interactive,
            gateway=self._deps.gateway,
            history=self._deps.history,
            media=self._deps.media,
            schema=self._deps.schema,
            sandbox=self._deps.sandbox,
            memory=self._deps.memory,
            tasks=self._deps.tasks,
            watches=self._deps.watches,
            permissions=self._deps.permissions,
            confirmations=self._deps.confirmations,
            scheduler_running=bool(self._deps.scheduler_running and self._deps.scheduler_running()),
            cancelled=cancel,
        )

        system = build_system_prompt(
            settings,
            now=datetime.now(UTC),
            account=self._deps.account,
            tool_names=self._registry.names(),
            interactive=interactive,
        )
        tools = self._registry.specs()

        # The operator's instruction is trusted input; it enters unfenced.
        history.append(Message.user(prompt))
        await self._persist(conversation.id, MessageRole.USER, history[-1])

        await emit(
            AgentEvent.make(
                EventKind.RUN_STARTED,
                data={"run_id": run_id, "conversation_id": conversation.id, "tools": len(tools)},
            )
        )

        usage = Usage()
        tool_calls_made = 0
        consecutive_errors = 0
        answer = ""
        stopped_because: str | None = None
        errors: list[str] = []
        step = 0

        for step in range(1, settings.agent.max_steps + 1):
            if cancel.is_set():
                stopped_because = "cancelled"
                break

            await emit(AgentEvent.make(EventKind.STEP_STARTED, data={"step": step}))

            history = await self._maybe_compact(history, system, tools, emit)

            try:
                async with asyncio.timeout(settings.agent.step_timeout):
                    completion = await self._ask_model(system, history, tools, emit)
            except TimeoutError:
                errors.append(f"Step {step} timed out.")
                stopped_because = "step_timeout"
                answer = answer or "A step took too long and the run was stopped."
                break
            except LLMError as exc:
                errors.append(str(exc))
                # A misconfiguration and an unreachable provider need different
                # things from whoever reads this: one means "change a setting",
                # the other means "try again later". Reporting both as "could not
                # reach" sends someone hunting a network fault they do not have.
                if isinstance(exc, LLMConfigError):
                    stopped_because = "llm_config_error"
                    answer = (
                        f"The language model is not configured correctly, so I could "
                        f"not start: {exc}\n\nRunning this again will not help until "
                        f"that is fixed. Nothing was changed on your Telegram account."
                    )
                else:
                    stopped_because = "llm_error"
                    answer = (
                        f"I could not reach the language model: {exc.user_message} "
                        f"Nothing was changed on your Telegram account by this failure."
                    )
                await emit(AgentEvent.make(EventKind.ERROR, answer))
                break

            usage = usage + completion.usage
            history.append(completion.message)
            await self._persist(conversation.id, MessageRole.ASSISTANT, completion.message)

            if text := completion.text.strip():
                answer = text
                await emit(AgentEvent.make(EventKind.ASSISTANT_MESSAGE, text))

            calls = completion.tool_calls
            if not calls:
                if completion.stop_reason is StopReason.MAX_TOKENS:
                    errors.append("The model's reply was cut off by the output limit.")
                break

            if tool_calls_made + len(calls) > settings.agent.max_tool_calls:
                stopped_because = "max_tool_calls"
                answer = answer or (
                    f"I stopped after {tool_calls_made} tool calls, the configured "
                    f"maximum for one run."
                )
                break

            results = await self._execute_tools(calls, tool_context, emit, cancel)
            tool_calls_made += len(calls)

            failed = sum(1 for r in results if r.is_error)
            consecutive_errors = consecutive_errors + 1 if failed == len(results) else 0
            if consecutive_errors >= settings.agent.max_consecutive_tool_errors:
                stopped_because = "repeated_tool_failures"
                answer = answer or (
                    "Several tool calls failed in a row, so I stopped rather than "
                    "keep retrying. The errors are above."
                )
                break

            tool_message = Message.tool_results(
                [
                    self._to_result_part(call, result)
                    for call, result in zip(calls, results, strict=True)
                ]
            )
            history.append(tool_message)
            await self._persist(conversation.id, MessageRole.TOOL, tool_message)
        else:
            stopped_because = "max_steps"
            answer = answer or (
                f"I reached the {settings.agent.max_steps}-step limit before finishing. "
                f"Here is what I had so far."
            )

        if cancel.is_set() and stopped_because is None:
            stopped_because = "cancelled"

        await self._deps_touch_conversation(conversation.id, prompt)

        result = RunResult(
            run_id=run_id,
            conversation_id=conversation.id,
            answer=answer or "(the model produced no final answer)",
            steps=step,
            tool_calls=tool_calls_made,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=(time.perf_counter() - started) * 1000,
            stopped_because=stopped_because,
            cancelled=cancel.is_set(),
            errors=errors,
        )
        await emit(AgentEvent.make(EventKind.RUN_FINISHED, result.answer, data={"result": result}))
        log.info(
            "agent.run_finished",
            steps=result.steps,
            tool_calls=result.tool_calls,
            tokens=result.input_tokens + result.output_tokens,
            stopped_because=stopped_because,
        )
        return result

    # --------------------------------------------------------------- model ---
    async def _ask_model(
        self,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> Completion:
        params = self._generation_params()

        if not self._settings.llm.stream:
            return await self._provider.complete(
                system=system, messages=history, tools=tools, params=params
            )

        completion: Completion | None = None
        async for event in self._provider.stream(
            system=system, messages=history, tools=tools, params=params
        ):
            if event.kind == "text" and event.text:
                await emit(AgentEvent.make(EventKind.TEXT_DELTA, event.text))
            elif event.kind == "thinking" and event.text:
                await emit(AgentEvent.make(EventKind.THINKING_DELTA, event.text))
            elif event.kind == "done":
                completion = event.completion

        if completion is None:
            raise LLMError("The provider stream ended without producing a completion.")
        return completion

    def _generation_params(self) -> GenerationParams:
        llm = self._settings.llm
        return GenerationParams(
            max_output_tokens=llm.max_output_tokens,
            temperature=llm.temperature,
            top_p=llm.top_p,
            effort=llm.effort,
            thinking=llm.thinking,
            extra=dict(llm.extra),
        )

    # --------------------------------------------------------------- tools ---
    async def _execute_tools(
        self,
        calls: Sequence[ToolCallPart],
        context: ToolContext,
        emit: Callable[[AgentEvent], Awaitable[None]],
        cancel: asyncio.Event,
    ) -> list[ToolResult]:
        """Run the requested tools, in parallel where that is safe.

        Parallelism is opt-out because most tool calls in a step are independent
        reads. Anything that changes state is serialised by the gateway's write
        throttle regardless, so ordering hazards do not accumulate here.
        """
        if not self._settings.agent.parallel_tool_calls or len(calls) == 1:
            return [await self._execute_one(call, context, emit, cancel) for call in calls]

        semaphore = asyncio.Semaphore(self._settings.agent.max_parallel_tools)

        async def guarded(call: ToolCallPart) -> ToolResult:
            async with semaphore:
                return await self._execute_one(call, context, emit, cancel)

        return list(await asyncio.gather(*(guarded(call) for call in calls)))

    async def _execute_one(
        self,
        call: ToolCallPart,
        context: ToolContext,
        emit: Callable[[AgentEvent], Awaitable[None]],
        cancel: asyncio.Event,
    ) -> ToolResult:
        if cancel.is_set():
            return ToolResult.error("Cancelled by the user before this tool ran.")

        await emit(
            AgentEvent.make(
                EventKind.TOOL_CALL_STARTED,
                data={"tool": call.name, "arguments": call.arguments, "id": call.id},
            )
        )
        started = time.perf_counter()

        try:
            tool = self._registry.get(call.name)
        except ToolNotFound as exc:
            return ToolResult.error(exc.user_message)

        try:
            async with asyncio.timeout(self._settings.agent.tool_timeout):
                result = await tool.run(call.arguments, context)
        except TimeoutError:
            result = ToolResult.error(
                f"{call.name} exceeded its {self._settings.agent.tool_timeout:.0f}s "
                f"time limit and was stopped."
            )
        except PermissionDenied as exc:
            # A refusal is information for the model, not a crash. It should
            # adapt or report, not retry the same call.
            result = ToolResult.error(f"Denied by policy: {exc.user_message}")
        except OperationCancelled:
            result = ToolResult.error("Cancelled by the user.")
        except TgAgentError as exc:
            result = ToolResult.error(f"{call.name} failed: {exc.user_message}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("agent.tool_crashed", tool=call.name, error=str(exc), exc_info=True)
            result = ToolResult.error(f"{call.name} raised an unexpected error: {exc}")

        elapsed = (time.perf_counter() - started) * 1000
        await emit(
            AgentEvent.make(
                EventKind.TOOL_CALL_FINISHED,
                data={
                    "tool": call.name,
                    "id": call.id,
                    "ok": not result.is_error,
                    "duration_ms": round(elapsed, 1),
                    "metadata": result.metadata,
                },
            )
        )
        return result

    def _to_result_part(self, call: ToolCallPart, result: ToolResult) -> ToolResultPart:
        """Render a tool result for the model, fencing it if it is untrusted."""
        content = result.content
        limit = self._settings.agent.max_tool_result_chars
        if len(content) > limit:
            # Keep both ends: the head usually carries structure, the tail
            # usually carries the cursor needed to continue.
            head, tail = content[: limit * 2 // 3], content[-(limit // 3) :]
            content = f"{head}\n\n… [{len(result.content) - limit} characters omitted] …\n\n{tail}"

        if result.trust is TrustLevel.UNTRUSTED:
            scan = result.metadata.get("scan")
            content = wrap_untrusted(
                UntrustedContent(
                    text=content,
                    source=result.source,
                    suspicion=float(result.metadata.get("max_suspicion", 0.0) or 0.0),
                    notes=tuple(scan) if isinstance(scan, (list, tuple)) else (),
                )
            )

        return ToolResultPart(tool_call_id=call.id, content=content, is_error=result.is_error)

    # ------------------------------------------------------------- context ---
    async def _maybe_compact(
        self,
        history: list[Message],
        system: str,
        tools: Sequence[Any],
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> list[Message]:
        if not self._context.needs_compaction(history, system=system, tools=tools):
            return history
        compacted, outcome = await self._context.compact(history, system=system, tools=tools)
        if outcome.compacted:
            await emit(
                AgentEvent.make(
                    EventKind.CONTEXT_COMPACTED,
                    f"Compacted {outcome.messages_before} turns into "
                    f"{outcome.messages_after} (~{outcome.tokens_before} → "
                    f"~{outcome.tokens_after} tokens).",
                    data={"tokens_before": outcome.tokens_before},
                )
            )
        else:
            await emit(AgentEvent.make(EventKind.WARNING, f"Could not compact: {outcome.reason}"))
        return compacted

    # ----------------------------------------------------------- persistence --
    async def _load_or_create_conversation(
        self, conversation_id: str | None, prompt: str
    ) -> Conversation:
        repo = self._deps.conversations
        if repo is None:
            return Conversation(id=conversation_id or uuid.uuid4().hex, title=prompt[:80])
        if conversation_id and (existing := await repo.get_conversation(conversation_id)):
            return existing
        conversation = Conversation(
            id=conversation_id or uuid.uuid4().hex, title=prompt[:80].strip()
        )
        return await repo.create_conversation(conversation)

    async def _load_history(self, conversation_id: str) -> list[Message]:
        repo = self._deps.conversations
        limit = self._settings.agent.history_limit
        if repo is None or limit <= 0:
            return []
        stored = await repo.get_messages(conversation_id, limit=limit)
        messages = [Message.from_dict(m.content) for m in stored if m.content]
        return _drop_dangling_tool_calls(messages)

    async def _persist(self, conversation_id: str, role: MessageRole, message: Message) -> None:
        repo = self._deps.conversations
        if repo is None:
            return
        with contextlib.suppress(Exception):
            await repo.add_message(
                StoredMessage(
                    conversation_id=conversation_id,
                    role=role,
                    content=message.to_dict(),
                    token_estimate=self._context.estimate([message]),
                )
            )

    async def _deps_touch_conversation(self, conversation_id: str, prompt: str) -> None:
        repo = self._deps.conversations
        if repo is None:
            return
        with contextlib.suppress(Exception):
            await repo.touch_conversation(conversation_id, title=prompt[:80].strip() or None)


# ---------------------------------------------------------------- helpers ----
def _make_emitter(callback: EventCallback | None) -> Callable[[AgentEvent], Awaitable[None]]:
    """Normalise sync/async/absent callbacks into one awaitable interface."""

    async def emit(event: AgentEvent) -> None:
        if callback is None:
            return
        try:
            outcome = callback(event)
            if asyncio.iscoroutine(outcome):
                await outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken UI must not kill a run
            log.warning("agent.event_callback_failed", error=str(exc))

    return emit


def _drop_dangling_tool_calls(messages: list[Message]) -> list[Message]:
    """Remove tool calls whose results were never persisted.

    A run interrupted mid-step leaves an assistant turn requesting tools with no
    matching results. Replaying that to a provider is a hard 400, so the orphan
    is rewritten into plain text that preserves what was attempted.
    """
    resolved: set[str] = set()
    for message in messages:
        for part in message.content:
            if isinstance(part, ToolResultPart):
                resolved.add(part.tool_call_id)

    cleaned: list[Message] = []
    for message in messages:
        calls = [p for p in message.content if isinstance(p, ToolCallPart)]
        if not calls or all(c.id in resolved for c in calls):
            cleaned.append(message)
            continue

        kept: list[ContentPart] = [p for p in message.content if not isinstance(p, ToolCallPart)]
        unanswered = ", ".join(c.name for c in calls if c.id not in resolved)
        kept.append(TextPart(f"[interrupted before these tools completed: {unanswered}]"))
        cleaned.append(Message(role=message.role, content=kept))

    # A tool-result turn whose request has just been rewritten is now an orphan
    # in the other direction; drop those too.
    requested: set[str] = {p.id for m in cleaned for p in m.content if isinstance(p, ToolCallPart)}
    final: list[Message] = []
    for message in cleaned:
        results = [p for p in message.content if isinstance(p, ToolResultPart)]
        if results and not any(r.tool_call_id in requested for r in results):
            continue
        final.append(message)
    return final
