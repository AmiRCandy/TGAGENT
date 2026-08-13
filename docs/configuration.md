# Configuration

Every tunable in the project is defined in
[`config/settings.py`](../src/tgagent/config/settings.py). Nothing else reads
`os.environ`, and nothing else hard-codes a limit.

## How settings are resolved

In order of precedence, highest first:

1. **Explicit arguments** — `Settings(...)`, or CLI flags like `--read-only`.
2. **Environment variables** — `TGAGENT_*`.
3. **`.env`** in the working directory.
4. **Defaults** from the model definitions.

Permission policy has one extra layer: a [YAML policy file](permissions.md) that
overrides the permission defaults.

### Naming

`TGAGENT_` prefix, `__` (double underscore) to descend into a section:

```bash
TGAGENT_LLM__MODEL=claude-opus-5              # settings.llm.model
TGAGENT_AGENT__MAX_STEPS=25                   # settings.agent.max_steps
TGAGENT_TELEGRAM__API_ID=1234567              # settings.telegram.api_id
TGAGENT_FEATURES__CODE_EXECUTION=false        # settings.features.code_execution
```

### Inspecting

```console
$ tgagent config show      # secrets masked
$ tgagent config check     # what is configured, what is missing
$ tgagent config policy    # the effective permission policy
```

## Sections

### `telegram`

| Setting | Default | Notes |
|---|---|---|
| `api_id` | `0` | From my.telegram.org/apps. Required. |
| `api_hash` | — | Secret. Required. |
| `phone` | — | E.164. Validated at load time. |
| `session_name` | `tgagent` | Session file stem |
| `session_dir` | `<data_dir>/sessions` | |
| `device_model` | `tgagent` | Shown in Telegram's Devices list |
| `connection_retries` | `5` | |
| `request_retries` | `3` | |
| `timeout` | `30.0` | Seconds per request |
| `flood_sleep_threshold` | `60` | `FLOOD_WAIT` below this is slept through silently; above it surfaces |
| `proxy` | — | `socks5://user:pass@host:1080`. Needs `pip install "tgagent[proxy]"` |

### `llm`

| Setting | Default | Notes |
|---|---|---|
| `provider` | `anthropic` | `anthropic` · `openai` · `openai-compatible` · `openrouter` · `ollama` · `fake` |
| `model` | `claude-opus-5` | |
| `api_key` | — | Falls back to the provider SDK's own env var |
| `base_url` | — | For OpenAI-compatible gateways |
| `max_output_tokens` | `8192` | |
| `temperature` | **unset** | Only sent when set — several current models reject it |
| `top_p` | **unset** | Same |
| `effort` | — | `low`…`max`, where supported |
| `thinking` | `true` | Extended/adaptive thinking where supported |
| `context_window` | `200000` | Used for compaction decisions, not sent to the provider |
| `timeout` | `180.0` | |
| `max_retries` | `4` | Exponential backoff with full jitter |
| `stream` | `true` | |
| `extra` | `{}` | Passed to the SDK untouched |

Set `context_window` to the real window of the model you chose — the compactor
uses it, and a wrong value means compacting too early or overflowing.

### `agent`

Execution budgets. These are the guardrails against a confused model looping.

| Setting | Default | Notes |
|---|---|---|
| `max_steps` | `25` | LLM round trips per run |
| `max_tool_calls` | `100` | |
| `max_consecutive_tool_errors` | `4` | Stops a retry loop |
| `step_timeout` | `300.0` | One LLM call plus its tools |
| `run_timeout` | `1800.0` | Whole run |
| `tool_timeout` | `120.0` | One tool call |
| `compaction_threshold` | `0.7` | Fraction of the window at which older turns are summarised |
| `compaction_keep_recent` | `6` | Never compacted away |
| `history_limit` | `40` | Prior turns reloaded from storage |
| `max_tool_result_chars` | `24000` | Longer results are truncated head-and-tail |
| `parallel_tool_calls` | `true` | |
| `max_parallel_tools` | `4` | |

### `permissions`

Summarised here; the full treatment is in [permissions](permissions.md).

| Setting | Default |
|---|---|
| `policy_file` | — |
| `defaults` | read/reversible **allow**, visible/destructive **confirm**, account-security **deny** |
| `method_overrides` | `{}` |
| `chat_allowlist` / `chat_denylist` | `[]` |
| `non_interactive_decision` | `deny` |
| `confirmation_timeout` | `300.0` |
| `read_only_mode` | `false` |
| `max_outbound_per_run` | `20` |
| `min_seconds_between_writes` | `1.0` |

A partial `defaults` mapping is **merged** with the conservative baseline rather
than replacing it, so tightening one tier cannot accidentally deny everything
else.

### `sandbox`

See [sandboxing](sandboxing.md) for what each backend guarantees.

| Setting | Default | Notes |
|---|---|---|
| `backend` | `subprocess` | `subprocess` · `docker` · `disabled` · `inprocess` (tests only) |
| `timeout` | `60.0` | Wall clock per execution |
| `max_output_bytes` | `256000` | |
| `max_memory_mb` | `512` | POSIX/docker only |
| `max_cpu_seconds` | `60` | POSIX/docker only |
| `allowed_imports` | 24 stdlib modules | No `os`, `sys`, `socket`, `subprocess`, `pathlib` |
| `max_rpc_calls` | `200` | Telegram calls per execution |
| `max_concurrent_rpc` | `4` | |
| `docker_image` | `python:3.12-slim` | |
| `docker_network` | `none` | |

### `storage`, `media`, `logging`, `scheduler`

| Setting | Default | Notes |
|---|---|---|
| `storage.database_path` | `<data_dir>/tgagent.db` | |
| `storage.audit_retention_days` | `90` | 0 disables pruning |
| `media.download_dir` | `<data_dir>/media` | |
| `media.max_file_bytes` | `100 MiB` | Checked *before* transfer |
| `media.allowed_mime_prefixes` | image/video/audio/text/pdf/… | Empty list means anything |
| `media.blocked_extensions` | `.exe`, `.dll`, `.ps1`, … | Checked in addition to MIME |
| `media.retention_days` | `7` | Downloads are reaped |
| `logging.level` | `INFO` | |
| `logging.format` | `console` | Use `json` when shipping logs |
| `logging.file` | — | Always JSON when set |
| `logging.log_call_arguments` | `false` | Arguments contain message text — user data |
| `scheduler.enabled` | `true` | |
| `scheduler.tick_interval` | `20.0` | |
| `scheduler.misfire_grace` | `900.0` | A run later than this is skipped, not fired |
| `scheduler.max_concurrent_tasks` | `2` | |
| `scheduler.default_timezone` | `UTC` | |

### `control`

Driving the agent from inside Telegram: `agent <instruction>` typed in any chat.
Off until `tgagent listen` (or `control.enabled` plus `tgagent serve`) runs it.
The security reasoning behind these defaults is in
[Telegram control](telegram-control.md) — read it before widening
`allowed_senders`, which is the one setting here that grants authority.

| Setting | Default | Notes |
|---|---|---|
| `control.enabled` | `false` | Start the bridge under `tgagent serve`; `listen` does not need it |
| `control.trigger` | `agent` | Whole word, start of message, case-insensitive |
| `control.respond_to_self` | `true` | Your own outgoing messages are commands |
| `control.allowed_senders` | `[]` | Others who may command — they act as your account |
| `control.allowed_chats` | `[]` | Non-empty restricts commands to these chats |
| `control.ignored_chats` | `[]` | Never accept commands here; wins over the allowlist |
| `control.reply_to_command` | `true` | Answer as a reply, not a loose message |
| `control.typing_indicator` | `true` | Cosmetic; failures never affect a run |
| `control.progress_updates` | `true` | Acknowledge at once, edit that message until the answer |
| `control.progress_interval` | `5.0` | Seconds between those edits |
| `control.include_reply_context` | `true` | Replied-to message, fenced as untrusted |
| `control.reply_context_chars` | `2000` | Cap on that context |
| `control.max_reply_chars` | `3800` | Longer answers are split; Telegram's limit is 4096 |
| `control.confirm_in_chat` | `true` | Otherwise CONFIRM falls to `non_interactive_decision` |
| `control.max_concurrent_runs` | `2` | One per chat regardless |
| `control.conversation_scope` | `chat` | `chat` \| `global` |
| `control.max_commands_per_minute` | `6` | Loop breaker, not a UX limit |

### `features`

Coarse switches. A disabled capability is **removed from the tool list** rather
than left in and made to fail — a tool the model can see is a tool it will try.

| Flag | Default |
|---|---|
| `code_execution` | `true` |
| `media_download` | `true` |
| `media_upload` | `false` |
| `scheduling` | `true` |
| `memory` | `true` |
| `injection_scanner` | `true` |

## Data directory

Defaults per platform:

- Linux: `$XDG_DATA_HOME/tgagent` or `~/.local/share/tgagent`
- macOS: `~/Library/Application Support/tgagent`
- Windows: `%APPDATA%\tgagent`

Override with `TGAGENT_DATA_DIR`. Everything unset defaults beneath it:

```
<data_dir>/
├── sessions/tgagent.session   ← authenticated credential (0700 dir, 0600 file)
├── tgagent.db                 ← conversations, memory, tasks, audit
├── media/<run-id>/            ← downloads, reaped on retention
└── cache/telethon-schema.json ← API index, rebuilt on Telethon upgrade
```

## Secrets

Secret fields are `SecretStr`: they do not appear in `repr()`, tracebacks, or
`model_dump()`. In addition:

- The composition root registers the **literal values** with the log redactor at
  startup, so any log line containing one is rewritten.
- Pattern-based redaction catches credential *shapes* the redactor was never
  told about — bot tokens, `sk-` keys, bearer headers, 32-char hex.
- `tgagent config show` masks anything whose key looks secret.
- The sandbox child gets an environment stripped to `PATH`, `TEMP`, `LANG` and a
  handful of others. No `ANTHROPIC_API_KEY`, no `TGAGENT_*`.

Never put secrets in the policy file — it is designed to be committed.

## Programmatic use

```python
from tgagent.config import load_settings, Settings

settings = load_settings()  # environment + .env
settings = load_settings(llm={"model": "x"})  # with overrides

settings = Settings(  # fully explicit, no environment
    data_dir="/srv/tgagent",
    telegram={"api_id": 1, "api_hash": "…"},
    llm={"provider": "fake"},
)
```
