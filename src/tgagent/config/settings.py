"""Typed, validated, environment-driven configuration.

Every tunable in the project lives here. Nothing else reads ``os.environ`` and
nothing else hard-codes a limit — that is the point.

Environment variables use the ``TGAGENT_`` prefix and ``__`` to descend into
nested sections::

    TGAGENT_TELEGRAM__API_ID=123456
    TGAGENT_LLM__MODEL=claude-opus-5
    TGAGENT_SANDBOX__BACKEND=docker

A ``.env`` file in the working directory is loaded automatically. Secrets are
held as :class:`~pydantic.SecretStr`, which keeps them out of ``repr()``,
tracebacks, and accidental log lines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tgagent.errors import ConfigError
from tgagent.risk import PolicyDecision, RiskTier


def default_data_dir() -> Path:
    """Per-user data directory, honouring XDG on POSIX and APPDATA on Windows."""
    if override := os.environ.get("TGAGENT_DATA_DIR"):
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tgagent"


class TelegramSettings(BaseModel):
    """MTProto client credentials and connection behaviour.

    ``api_id`` and ``api_hash`` come from https://my.telegram.org/apps and
    identify *the application*, not the account. They are still secrets.
    """

    api_id: int = Field(default=0, description="API ID from my.telegram.org/apps")
    api_hash: SecretStr = Field(default=SecretStr(""), description="API hash")
    phone: str | None = Field(default=None, description="E.164 phone number, e.g. +15551234567")

    session_name: str = Field(default="tgagent", description="Session file stem")
    session_dir: Path | None = Field(
        default=None, description="Directory holding session files; defaults to <data_dir>/sessions"
    )

    # Presented to Telegram in the "active sessions" list. Being honest here is
    # deliberate: the account owner should be able to recognise this client.
    device_model: str = Field(default="tgagent")
    system_version: str = Field(default="1.0")
    app_version: str = Field(default="tgagent")
    lang_code: str = Field(default="en")

    connection_retries: int = Field(default=5, ge=0, le=100)
    request_retries: int = Field(default=3, ge=0, le=20)
    retry_delay: float = Field(default=1.0, ge=0.0, le=60.0)
    timeout: float = Field(default=30.0, gt=0, le=600)

    #: FLOOD_WAIT values below this are slept through transparently by Telethon.
    #: Above it, the error surfaces so the agent can decide what to do.
    flood_sleep_threshold: int = Field(default=60, ge=0, le=3600)

    #: e.g. "socks5://user:pass@host:1080". Parsed by :func:`parse_proxy`.
    proxy: SecretStr | None = Field(default=None)

    #: Wait at most this long for an interactive code/password during login.
    login_timeout: float = Field(default=300.0, gt=0)

    #: How often to look at whether updates are still arriving. Cheap: it only
    #: reads a timestamp unless the connection has gone quiet.
    health_check_interval: float = Field(default=60.0, ge=5.0, le=3600.0)
    #: Quiet for this long and the connection is actively probed. A socket can
    #: die without either side noticing — a NAT or conntrack table drops the flow
    #: with no RST — and the result is a client that reports itself connected,
    #: never fires `disconnected`, and silently receives nothing again. Fifteen
    #: to thirty minutes is the classic interval for that on a VPS, which is
    #: exactly when a listener appears to be running and answers nothing.
    idle_probe_after: float = Field(default=300.0, ge=30.0, le=86_400.0)
    #: Consecutive failed recoveries before giving up and letting the supervisor
    #: restart the process. Exiting beats pretending to run.
    max_recovery_attempts: int = Field(default=5, ge=1, le=100)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().replace(" ", "").replace("-", "")
        if not v.startswith("+") or not v[1:].isdigit() or not 7 <= len(v) <= 16:
            raise ValueError("phone must be E.164, e.g. +15551234567")
        return v

    def is_configured(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash.get_secret_value())


class LLMSettings(BaseModel):
    """Model provider selection and generation parameters.

    ``provider`` names an entry in :mod:`tgagent.llm.registry`; nothing about the
    rest of the system knows which vendor is in use.
    """

    provider: str = Field(default="anthropic", description="Registered provider name")
    model: str = Field(default="claude-opus-5")
    api_key: SecretStr | None = Field(default=None)
    base_url: str | None = Field(
        default=None, description="Override the provider endpoint (OpenAI-compatible gateways)"
    )

    max_output_tokens: int = Field(default=32768, ge=64, le=200_000)

    #: ``None`` means "do not send the parameter". Several current models reject
    #: sampling parameters outright, so an explicit opt-in is the only safe default.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)

    #: Reasoning-effort hint. Providers that don't support it ignore it.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(default=None)
    thinking: bool = Field(
        default=True, description="Enable extended/adaptive thinking if supported"
    )

    #: Used for budgeting and compaction decisions, not sent to the provider.
    context_window: int = Field(default=200_000, ge=4_000)

    timeout: float = Field(default=180.0, gt=0, le=3600)
    max_retries: int = Field(default=4, ge=0, le=10)
    retry_base_delay: float = Field(default=1.0, gt=0, le=30)
    retry_max_delay: float = Field(default=30.0, gt=0, le=300)

    stream: bool = Field(
        default=True, description="Stream responses where the provider supports it"
    )

    #: Ask the provider to cache the unchanging prefix of each request — the tool
    #: schemas and the system prompt, together some 9k tokens that are otherwise
    #: re-read on every step of every run. Anthropic needs to be asked explicitly
    #: (this is that ask); OpenAI-compatible endpoints do it themselves and ignore
    #: the setting. Off is for a gateway that rejects the field.
    prompt_caching: bool = Field(default=True)

    #: Extra provider-specific keyword arguments, passed through untouched.
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentSettings(BaseModel):
    """Execution budgets for a single agent run.

    These are the guardrails that stop a confused model from looping forever or
    spending an unbounded amount of money.
    """

    max_steps: int = Field(default=25, ge=1, le=500, description="LLM round trips per run")
    max_tool_calls: int = Field(default=200, ge=1, le=2000)
    max_consecutive_tool_errors: int = Field(default=4, ge=1, le=50)

    step_timeout: float = Field(default=300.0, gt=0, description="Seconds for one LLM+tools step")
    run_timeout: float = Field(default=1800.0, gt=0, description="Seconds for a whole run")
    tool_timeout: float = Field(default=120.0, gt=0, description="Default per-tool-call timeout")

    #: Fraction of the context window at which older turns get compacted.
    compaction_threshold: float = Field(default=0.7, gt=0.1, le=0.95)
    #: Never compact away the most recent N messages — they carry the live task.
    compaction_keep_recent: int = Field(default=6, ge=2, le=100)

    #: How many prior turns of an agent conversation to reload from storage.
    history_limit: int = Field(default=40, ge=0, le=1000)

    #: Cap on a single tool result before it is truncated, in characters.
    max_tool_result_chars: int = Field(default=24_000, ge=500, le=500_000)

    parallel_tool_calls: bool = Field(default=True)
    max_parallel_tools: int = Field(default=6, ge=1, le=32)


class PermissionSettings(BaseModel):
    """Permission policy: defaults, overrides, and where the YAML lives."""

    policy_file: Path | None = Field(default=None, description="YAML policy; overrides defaults")

    #: Baseline decision per risk tier. Deliberately conservative.
    defaults: dict[RiskTier, PolicyDecision] = Field(
        default_factory=lambda: {
            RiskTier.READ_ONLY: PolicyDecision.ALLOW,
            RiskTier.REVERSIBLE: PolicyDecision.ALLOW,
            RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.CONFIRM,
            RiskTier.DESTRUCTIVE: PolicyDecision.CONFIRM,
            RiskTier.ACCOUNT_SECURITY: PolicyDecision.DENY,
        }
    )

    #: Per-method overrides, e.g. ``{"messages.SendMessage": "deny"}``.
    method_overrides: dict[str, PolicyDecision] = Field(default_factory=dict)

    #: If non-empty, only these chat identifiers may be *written to*.
    chat_allowlist: list[str] = Field(default_factory=list)
    #: Chats that may never be written to. Takes precedence over the allowlist.
    chat_denylist: list[str] = Field(default_factory=list)

    #: With no interactive user attached (scheduled runs), CONFIRM becomes this.
    non_interactive_decision: PolicyDecision = Field(default=PolicyDecision.DENY)

    #: Seconds to wait at a confirmation prompt before treating it as declined.
    confirmation_timeout: float = Field(default=300.0, gt=0)

    #: Global kill switch: force every write operation to DENY.
    read_only_mode: bool = Field(default=False)

    #: Cap on outbound messages per run, as a blast-radius limit independent of
    #: per-call confirmation.
    max_outbound_per_run: int = Field(default=20, ge=0, le=1000)

    #: Minimum spacing between externally-visible operations. Protects the
    #: account from tripping Telegram's spam heuristics if the agent loops.
    min_seconds_between_writes: float = Field(default=1.0, ge=0.0, le=60.0)

    @model_validator(mode="after")
    def _fill_missing_tiers(self) -> PermissionSettings:
        """Ensure every risk tier has a decision.

        A caller (or a policy file) that specifies only some tiers must not
        silently leave the rest undefined — the lookup would fall through to
        DENY and, say, break all reads because someone tightened `destructive`.
        Missing tiers inherit the conservative baseline instead.
        """
        baseline = {
            RiskTier.READ_ONLY: PolicyDecision.ALLOW,
            RiskTier.REVERSIBLE: PolicyDecision.ALLOW,
            RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.CONFIRM,
            RiskTier.DESTRUCTIVE: PolicyDecision.CONFIRM,
            RiskTier.ACCOUNT_SECURITY: PolicyDecision.DENY,
        }
        for tier, decision in baseline.items():
            self.defaults.setdefault(tier, decision)
        return self


class SandboxSettings(BaseModel):
    """Code-execution isolation.

    ``backend`` picks the strategy; see ``docs/sandboxing.md`` for exactly what
    each one does and does not guarantee on each platform.
    """

    backend: Literal["subprocess", "docker", "inprocess", "disabled"] = Field(default="subprocess")

    timeout: float = Field(default=60.0, gt=0, le=900, description="Wall clock per execution")
    max_output_bytes: int = Field(default=256_000, ge=1_000, le=10_000_000)
    max_memory_mb: int = Field(default=512, ge=64, le=8192)
    max_cpu_seconds: int = Field(default=60, ge=1, le=900)

    #: Modules generated code may import. Anything else raises inside the worker.
    allowed_imports: list[str] = Field(
        default_factory=lambda: [
            "json",
            "re",
            "math",
            "statistics",
            "datetime",
            "time",
            "collections",
            "itertools",
            "functools",
            "operator",
            "string",
            "textwrap",
            "typing",
            "dataclasses",
            "enum",
            "decimal",
            "fractions",
            "random",
            "uuid",
            "hashlib",
            "base64",
            "csv",
            "difflib",
            "unicodedata",
            "zoneinfo",
        ]
    )

    #: Docker-specific. Only consulted when ``backend == "docker"``.
    docker_image: str = Field(default="python:3.12-slim")
    docker_network: str = Field(default="none")
    docker_extra_args: list[str] = Field(default_factory=list)

    #: Max concurrent RPC calls a single execution may have in flight.
    max_concurrent_rpc: int = Field(default=4, ge=1, le=32)
    #: Hard cap on total RPC calls in one execution — stops runaway loops.
    max_rpc_calls: int = Field(default=200, ge=1, le=10_000)


class StorageSettings(BaseModel):
    """Where persistent state lives."""

    database_path: Path | None = Field(
        default=None, description="Defaults to <data_dir>/tgagent.db"
    )
    #: Retention for the audit log. 0 disables pruning.
    audit_retention_days: int = Field(default=90, ge=0, le=3650)
    busy_timeout_ms: int = Field(default=5000, ge=0)


class MediaSettings(BaseModel):
    """Download policy for Telegram media."""

    download_dir: Path | None = Field(default=None, description="Defaults to <data_dir>/media")
    max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    #: MIME prefixes that may be downloaded. Empty list means "anything".
    allowed_mime_prefixes: list[str] = Field(
        default_factory=lambda: [
            "image/",
            "video/",
            "audio/",
            "text/",
            "application/pdf",
            "application/json",
            "application/zip",
            "application/vnd.openxmlformats",
            "application/msword",
        ]
    )
    #: Extensions that are never written to disk regardless of MIME type.
    blocked_extensions: list[str] = Field(
        default_factory=lambda: [
            ".exe",
            ".dll",
            ".scr",
            ".com",
            ".pif",
            ".bat",
            ".cmd",
            ".ps1",
            ".vbs",
            ".js",
            ".jse",
            ".msi",
            ".msp",
            ".hta",
            ".cpl",
            ".lnk",
            ".jar",
            ".apk",
            ".app",
            ".dmg",
            ".sh",
        ]
    )
    retention_days: int = Field(default=7, ge=0, le=3650)


class LoggingSettings(BaseModel):
    """Structured logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    format: Literal["console", "json"] = Field(default="console")
    file: Path | None = Field(default=None, description="Also write JSON lines here")
    #: Log the arguments of Telegram calls. Off by default: arguments contain
    #: message text, which is user data.
    log_call_arguments: bool = Field(default=False)


class TelegramControlSettings(BaseModel):
    """Driving the agent from inside Telegram itself.

    When the bridge is listening, typing ``agent summarise the last 20 messages``
    in any chat hands that instruction to the agent, together with the chat and
    message it was typed in, and the answer comes back as a reply.

    The defaults are the conservative reading of that idea: only the account
    owner's own messages count as commands, and the whole thing is off until it
    is turned on. See ``docs/telegram-control.md``.
    """

    #: Start the bridge as part of ``tgagent serve``. ``tgagent listen`` starts
    #: it regardless — this flag is about the unattended daemon.
    enabled: bool = Field(default=False)

    #: The word that turns a message into an instruction. Matched
    #: case-insensitively at the very start of the message, and it must be
    #: followed by the instruction, so ordinary sentences do not trigger.
    trigger: str = Field(default="agent", min_length=1, max_length=32)

    #: Treat the account owner's own outgoing messages as commands. This is the
    #: normal mode: you type in a chat you are already in.
    respond_to_self: bool = Field(default=True)

    #: Other people whose messages count as commands — ``@username`` or numeric
    #: id. Empty means nobody else, which is the safe default: anyone on this
    #: list can spend your tokens and act as your account.
    allowed_senders: list[str] = Field(default_factory=list)

    #: If non-empty, commands are only accepted in these chats.
    allowed_chats: list[str] = Field(default_factory=list)
    #: Chats where commands are never accepted. Takes precedence.
    ignored_chats: list[str] = Field(default_factory=list)

    #: Send the answer as a reply to the command, rather than a loose message.
    reply_to_command: bool = Field(default=True)
    #: Show the "typing…" indicator while a run is in progress.
    typing_indicator: bool = Field(default=True)

    #: Acknowledge a command immediately with a status message, keep editing it
    #: while the run is in flight, and finally edit the answer into it. Without
    #: this a slow run looks exactly like a bridge that died. Turn it off and the
    #: answer arrives as a fresh message instead — which is the one thing an edit
    #: does not do: Telegram does not notify for edits.
    progress_updates: bool = Field(default=True)
    #: How often that status message is rewritten, in seconds. Every edit is an
    #: API call, so this is a floor on how much chatter a long run costs.
    progress_interval: float = Field(default=5.0, ge=1.0, le=60.0)

    #: Include the message the command replied to, as fenced untrusted context.
    #: This is what makes ``agent translate this`` work.
    include_reply_context: bool = Field(default=True)
    reply_context_chars: int = Field(default=2000, ge=0, le=20_000)

    #: Telegram's own hard limit is 4096 characters; longer answers are split.
    max_reply_chars: int = Field(default=3800, ge=200, le=4096)

    #: Ask for confirmations in the chat the command came from, by replying
    #: ``yes`` or ``no``. With this off, a CONFIRM decision falls through to
    #: ``permissions.non_interactive_decision`` (deny, by default).
    confirm_in_chat: bool = Field(default=True)

    #: Runs in flight across all chats. One chat runs one command at a time
    #: regardless, because a chat's conversation history is a single thread.
    max_concurrent_runs: int = Field(default=2, ge=1, le=16)

    #: ``chat`` gives every chat its own conversation, so follow-ups in that
    #: chat keep their context. ``global`` puts every chat in one conversation.
    conversation_scope: Literal["chat", "global"] = Field(default="chat")

    #: Hard ceiling on accepted commands per minute, across all chats. This is a
    #: loop breaker, not a UX limit: if the agent ever sends a message that is
    #: itself a command, this is what stops it running away.
    max_commands_per_minute: int = Field(default=6, ge=1, le=120)

    @field_validator("trigger")
    @classmethod
    def _clean_trigger(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("trigger must not be blank")
        return v


class AutoReplySettings(BaseModel):
    """Answering other people's messages on the account owner's behalf.

    A *watch* is a standing instruction bound to one chat — "reply to Alex the
    way I would while I am in the air" — that turns each arriving message in that
    chat into an agent run whose answer is sent back as the account.

    This is the only path in the system where the account speaks to somebody else
    without a per-message confirmation, so it is off until it is turned on, every
    watch expires on its own, and the limits below bound what a mistake costs.
    Read ``docs/autoreply.md`` before enabling it.
    """

    #: Off by default. Turning this on means arriving messages can cause the
    #: account to send messages, decided by a model, with nobody watching.
    enabled: bool = Field(default=False)

    #: Watches that may exist at once. Each one is a chat that can be answered
    #: without you.
    max_watches: int = Field(default=5, ge=1, le=100)

    #: How long a watch lives when the instruction does not say. A standing
    #: instruction to speak as you is not something to leave running by accident,
    #: so it ends on its own unless renewed.
    default_ttl_minutes: int = Field(default=240, ge=1, le=100_000)
    #: Ceiling on any requested lifetime, whatever the instruction asks for.
    max_ttl_minutes: int = Field(default=10_080, ge=1, le=525_600)

    #: Replies one watch may send before it stops. The default is a conversation,
    #: not a correspondence.
    max_replies_per_watch: int = Field(default=20, ge=1, le=1000)
    #: Replies across every watch in a rolling hour. The loop breaker: two
    #: accounts both running this would otherwise talk to each other forever.
    max_replies_per_hour: int = Field(default=30, ge=1, le=500)
    #: Minimum gap between two replies in the same chat, in seconds. Also
    #: collapses a burst of messages into one answer.
    cooldown_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)

    #: Prepended to every automatic reply. Empty by default because the point is
    #: usually to sound like you — but the person on the other end is talking to
    #: a model believing they are talking to you, and some jurisdictions require
    #: that to be disclosed. ``"🤖 "`` is a reasonable value.
    prefix: str = Field(default="", max_length=64)

    #: Show "typing…" in the watched chat while the reply is being written.
    typing_indicator: bool = Field(default=True)


class PluginSettings(BaseModel):
    """Tools somebody else wrote. See ``docs/plugins.md``.

    A plugin runs in this process with this account's credentials — it is not the
    sandbox. These settings bound the surface: what may be installed, from where,
    and how much of the model's tool list any one plugin can occupy.
    """

    #: Master switch. Off means no plugin tools at all, built-in ones included.
    enabled: bool = Field(default=True)
    #: Whether the plugins that ship with tgagent start switched on. They still
    #: do nothing until their requirements are installed and configured.
    builtins_enabled: bool = Field(default=True)
    #: Whether `agent plugin add` may fetch code at all. Off makes the set of
    #: plugins whatever is already on disk — a reasonable stance for a shared or
    #: unattended deployment.
    allow_install: bool = Field(default=True)
    #: Hosts a plugin may be installed from. https only, checked before fetching.
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["github.com", "gitlab.com", "codeberg.org"]
    )
    max_installed: int = Field(default=20, ge=1, le=200)
    #: One plugin should not be able to double the tool array on its own; every
    #: schema is re-read on every request.
    max_tools_per_plugin: int = Field(default=12, ge=1, le=64)


class SchedulerSettings(BaseModel):
    """Background task scheduling."""

    enabled: bool = Field(default=True)
    tick_interval: float = Field(default=20.0, gt=0, le=3600)
    #: A run more than this many seconds late is skipped rather than fired.
    misfire_grace: float = Field(default=900.0, ge=0)
    max_concurrent_tasks: int = Field(default=2, ge=1, le=32)
    default_timezone: str = Field(default="UTC")


class FeatureFlags(BaseModel):
    """Coarse on/off switches for capability groups."""

    code_execution: bool = Field(default=True)
    media_download: bool = Field(default=True)
    media_upload: bool = Field(default=False)
    scheduling: bool = Field(default=True)
    memory: bool = Field(default=True)
    #: Scan untrusted content for prompt-injection patterns and annotate it.
    injection_scanner: bool = Field(default=True)


class Settings(BaseSettings):
    """Root configuration object."""

    model_config = SettingsConfigDict(
        env_prefix="TGAGENT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    data_dir: Path = Field(default_factory=default_data_dir)

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    control: TelegramControlSettings = Field(default_factory=TelegramControlSettings)
    autoreply: AutoReplySettings = Field(default_factory=AutoReplySettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """Fill in every path that defaults to a location under ``data_dir``."""
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.telegram.session_dir is None:
            self.telegram.session_dir = self.data_dir / "sessions"
        if self.storage.database_path is None:
            self.storage.database_path = self.data_dir / "tgagent.db"
        if self.media.download_dir is None:
            self.media.download_dir = self.data_dir / "media"
        self.telegram.session_dir = self.telegram.session_dir.expanduser().resolve()
        self.storage.database_path = self.storage.database_path.expanduser().resolve()
        self.media.download_dir = self.media.download_dir.expanduser().resolve()
        return self

    @property
    def session_path(self) -> Path:
        assert self.telegram.session_dir is not None  # set by _resolve_paths
        return self.telegram.session_dir / f"{self.telegram.session_name}.session"

    @property
    def schema_cache_path(self) -> Path:
        return self.data_dir / "cache" / "telethon-schema.json"

    def ensure_directories(self) -> None:
        """Create the runtime directories, tightening permissions on POSIX."""
        assert self.telegram.session_dir and self.storage.database_path and self.media.download_dir
        for path, private in (
            (self.data_dir, True),
            (self.telegram.session_dir, True),
            (self.storage.database_path.parent, True),
            (self.media.download_dir, False),
            (self.schema_cache_path.parent, False),
        ):
            path.mkdir(parents=True, exist_ok=True)
            if private and os.name != "nt":
                path.chmod(0o700)

    def require_telegram(self) -> None:
        """Fail fast, with an actionable message, if credentials are absent."""
        if not self.telegram.is_configured():
            raise ConfigError(
                "Telegram credentials are missing. Set TGAGENT_TELEGRAM__API_ID and "
                "TGAGENT_TELEGRAM__API_HASH (get them from https://my.telegram.org/apps). "
                "See docs/telegram-setup.md."
            )


def load_settings(**overrides: Any) -> Settings:
    """Build :class:`Settings` from the environment, applying explicit overrides.

    The runtime-settable subset in :mod:`tgagent.config.local` is applied last, and
    from the *resolved* data directory rather than a path fixed at import time —
    which is what keeps a developer's real overrides file out of the test suite.
    """
    from tgagent.config.local import apply_local_overrides

    return apply_local_overrides(Settings(**overrides))
