"""The agent runtime: the plan/act/observe loop and its supporting machinery."""

from tgagent.agent.context import CompactionOutcome, ContextManager
from tgagent.agent.events import AgentEvent, EventKind, RunResult
from tgagent.agent.prompts import COMPACTION_PROMPT, build_system_prompt
from tgagent.agent.runtime import AgentRuntime, RuntimeDependencies

__all__ = [
    "COMPACTION_PROMPT",
    "AgentEvent",
    "AgentRuntime",
    "CompactionOutcome",
    "ContextManager",
    "EventKind",
    "RunResult",
    "RuntimeDependencies",
    "build_system_prompt",
]
