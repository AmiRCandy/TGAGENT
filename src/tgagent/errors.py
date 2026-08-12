"""The project's exception hierarchy.

Every error raised deliberately by tgagent derives from :class:`TgAgentError`, so
callers can distinguish "this subsystem said no" from "something unexpected
broke". Errors carry a ``user_message`` that is safe to show and safe to hand to
the model; ``str(exc)`` may contain more detail for logs.
"""

from __future__ import annotations

from typing import Any


class TgAgentError(Exception):
    """Base class for every error this project raises on purpose."""

    #: Shown to the user and fed back to the model. Must never contain secrets.
    user_message: str = "An internal error occurred."

    def __init__(self, message: str | None = None, **context: Any) -> None:
        self.context: dict[str, Any] = context
        self.user_message = message or self.user_message
        super().__init__(self.user_message)


# ----------------------------------------------------------------- config ---
class ConfigError(TgAgentError):
    """Configuration is missing, malformed, or internally inconsistent."""

    user_message = "Configuration error."


class PolicyError(ConfigError):
    """The permission policy file could not be loaded or is invalid."""

    user_message = "Permission policy error."


# --------------------------------------------------------------- telegram ---
class TelegramError(TgAgentError):
    """Base for Telegram-layer failures."""

    user_message = "Telegram operation failed."


class AuthenticationError(TelegramError):
    """Sign-in could not be completed."""

    user_message = "Telegram authentication failed."


class NotAuthorizedError(TelegramError):
    """No valid session; the account must be signed in first."""

    user_message = "Not signed in to Telegram. Run `tgagent login` first."


class TelegramCallError(TelegramError):
    """A Telegram API call was attempted and failed.

    ``method`` is the logical method name so the audit trail and the model both
    know which call failed, and ``retryable`` tells the runtime whether trying
    again could plausibly help.
    """

    user_message = "The Telegram API call failed."

    def __init__(
        self,
        message: str,
        *,
        method: str,
        retryable: bool = False,
        retry_after: float | None = None,
        **context: Any,
    ) -> None:
        self.method = method
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message, method=method, **context)


class EntityResolutionError(TelegramError):
    """A peer reference could not be resolved to a Telegram entity."""

    user_message = "Could not resolve that chat or user."


# --------------------------------------------------------------- security ---
class SecurityError(TgAgentError):
    """Base for security-layer refusals."""

    user_message = "Blocked by the security policy."


class PermissionDenied(SecurityError):
    """The permission engine refused an operation outright."""

    user_message = "That operation is not permitted by the current policy."

    def __init__(self, message: str, *, method: str, risk: str, **context: Any) -> None:
        self.method = method
        self.risk = risk
        super().__init__(message, method=method, risk=risk, **context)


class ConfirmationDenied(SecurityError):
    """A confirmation prompt was shown and the user declined (or it timed out)."""

    user_message = "The user did not approve that operation."


class SecretLeakError(SecurityError):
    """A value that looks like a credential was about to cross a boundary."""

    user_message = "Blocked: the payload appeared to contain a credential."


# ---------------------------------------------------------------- sandbox ---
class SandboxError(TgAgentError):
    """Base for code-execution failures."""

    user_message = "Sandboxed execution failed."


class SandboxTimeout(SandboxError):
    """Generated code exceeded its wall-clock budget."""

    user_message = "The code took too long and was terminated."


class SandboxUnavailable(SandboxError):
    """The configured sandbox backend cannot be started on this host."""

    user_message = "The sandbox backend is unavailable."


class SandboxProtocolError(SandboxError):
    """The worker sent a frame the host could not interpret."""

    user_message = "Internal sandbox protocol error."


# ------------------------------------------------------------------- llm ----
class LLMError(TgAgentError):
    """Base for model-provider failures."""

    user_message = "The language model request failed."


class LLMConfigError(ConfigError, LLMError):
    """The provider is unknown, misconfigured, or was asked for something it lacks.

    Deliberately *both* a ``ConfigError`` and an ``LLMError``, because it is
    genuinely both and each base is load-bearing somewhere:

    * ``ConfigError`` is how the CLI and the composition root recognise "the
      operator has to change something", as distinct from a transient failure.
    * ``LLMError`` is what the agent loop catches around a model call. Inheriting
      from ``ConfigError`` alone meant a wrong API key or an unavailable model
      escaped that handler entirely: :meth:`AgentRuntime.run` raised instead of
      returning a ``RunResult``, so the user's turn was persisted with no
      assistant turn, no ``ERROR`` event was emitted, and any interface waiting
      for ``RUN_FINISHED`` — the CLI renderer, the Telegram bridge's typing
      indicator — waited forever.

    Not retryable; see :class:`LLMTransientError` for the ones that are.
    """

    user_message = "LLM provider is not configured correctly."


class LLMTransientError(LLMError):
    """A retryable provider failure (rate limit, overload, network blip)."""

    user_message = "The language model is temporarily unavailable."

    def __init__(self, message: str, *, retry_after: float | None = None, **ctx: Any) -> None:
        self.retry_after = retry_after
        super().__init__(message, **ctx)


class ContextOverflowError(LLMError):
    """The conversation cannot be compacted small enough to fit the window."""

    user_message = "The conversation is too large for this model's context window."


# ------------------------------------------------------------------ tools ---
class ToolError(TgAgentError):
    """A tool failed in a way the model should see and can react to."""

    user_message = "The tool call failed."


class ToolNotFound(ToolError):
    """The model asked for a tool that is not registered."""

    user_message = "No such tool."


class ToolInputError(ToolError):
    """Tool arguments failed validation."""

    user_message = "Invalid tool arguments."


# --------------------------------------------------------------- runtime ----
class RuntimeLimitExceeded(TgAgentError):
    """An execution budget (steps, tool calls, wall clock) was exhausted."""

    user_message = "The agent hit an execution limit before finishing."


class OperationCancelled(TgAgentError):
    """The run was cancelled by the user or by shutdown."""

    user_message = "Cancelled."


# --------------------------------------------------------------- storage ----
class StorageError(TgAgentError):
    """Persistence-layer failure."""

    user_message = "Storage error."


class MigrationError(StorageError):
    """The database schema could not be brought to the expected version."""

    user_message = "Database migration failed."


# ------------------------------------------------------------- scheduler ----
class SchedulerError(TgAgentError):
    """Scheduling failure (bad cron expression, unknown task, …)."""

    user_message = "Scheduler error."


# ------------------------------------------------------------------ media ---
class MediaError(TgAgentError):
    """Media download/validation failure."""

    user_message = "Media handling failed."


class MediaTooLarge(MediaError):
    """The file exceeds the configured size cap."""

    user_message = "That file is larger than the configured download limit."


class MediaTypeRejected(MediaError):
    """The file's MIME type is not on the allow-list."""

    user_message = "That file type is not allowed by the current configuration."
