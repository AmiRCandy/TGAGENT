# Troubleshooting

Start here:

```console
$ tgagent config check      # what is configured, what is missing
$ tgagent sandbox           # what the sandbox isolates, with live probes
$ tgagent audit -n 20       # what actually happened
```

More detail:

```bash
TGAGENT_LOGGING__LEVEL=DEBUG tgagent run "…"
```

Logs are redacted automatically, but read them before pasting anywhere.

---

## Setup

### `Telegram credentials are missing`

Set `TGAGENT_TELEGRAM__API_ID` and `__API_HASH` from
<https://my.telegram.org/apps>. See [Telegram setup](telegram-setup.md).

### `.env` seems to be ignored

It is read from the **current working directory**, not the install location. Use
real environment variables, or `cd` to where the file is.

### `Unknown LLM provider 'anthropic'` / `requires the SDK`

The adapter is there but its SDK is not:

```bash
pip install "tgagent[anthropic]"     # or [openai]
```

### `Unknown timezone 'Europe/London'`

`zoneinfo` reads the *system* tz database, which Windows lacks and slim Linux
images often omit. `tzdata` is a hard dependency for exactly this reason, so if
you see it, reinstall:

```bash
pip install --force-reinstall tzdata
```

---

## Authentication

### `Not authorised. Run tgagent login first.`

No session, or it was revoked. Check Telegram → Settings → Devices; if `tgagent`
is not listed, sign in again.

### `That login code is incorrect` / `has expired`

Codes are short-lived and single-use. Start again. Note the code arrives **in
Telegram**, not by SMS, if you have another client signed in.

### `Too many login attempts`

A `FLOOD_WAIT`. Wait the stated time — retrying sooner extends it.

### The 2FA prompt never appears

It only appears when Telegram asks, i.e. when the account has a cloud password.
If you have one and are not prompted, you are probably signing into a different
account than you think.

### `No Telegram account exists for that number`

tgagent deliberately does not create accounts. Sign up in an official client
first.

---

## During a run

### "That operation is not permitted by the current policy"

Working as designed. `tgagent config policy <method>` shows why:

```console
$ tgagent config policy messages.DeleteHistory
Risk tier: destructive
Decision : confirm
```

To change it, edit the [policy file](permissions.md) — deliberately, not by
reflex.

### The agent keeps asking for confirmation

Every externally-visible and destructive operation confirms by default. If a task
legitimately needs several, approve one and answer yes to "allow further calls of
this type for the rest of this run".

### A scheduled task does nothing but report refusals

Expected. Scheduled runs are unattended, so `confirm` becomes `deny`. Grant what
that task needs explicitly and narrowly — see
[scheduling](scheduling.md#unattended-semantics--read-this).

### `Could not resolve '@someone'`

Telegram only lets you resolve peers you have some relationship with. Ask the
agent to list your dialogs first — that caches the reference. A numeric id also
works where a username does not.

### `Telegram rate-limited …; it asks to wait N seconds`

A `FLOOD_WAIT`. Waits under 60s are slept through transparently; longer ones
surface. If they are frequent, you are asking for too much too fast — lower page
sizes, and leave `min_seconds_between_writes` alone.

### "I reached the 25-step limit"

Either the request is genuinely large, or the agent is going in circles.

- Narrow the request ("January", not "recently").
- Suggest the approach ("search for X" rather than "look through everything").
- Raise `TGAGENT_AGENT__MAX_STEPS` if the task really is that big.

Circling usually means it is paginating when it should be searching, or the model
is weak at tool selection.

### "The conversation is too large for this model's context window"

Compaction could not get it under the limit. Start a new conversation (drop
`-c`), lower page sizes, or check that `llm.context_window` matches the model you
actually configured — a wrong value here causes exactly this.

### The run stopped after repeated tool failures

Four consecutive failures stop the run. The errors are in the output; the audit
log has the detail.

---

## Sandbox

### "Code execution is disabled in this deployment"

`features.code_execution=false` or `sandbox.backend=disabled`. The curated tools
and `telegram_invoke` still reach the whole API.

### `docker` backend: "the docker executable was not found"

Install Docker, or switch to `subprocess`.

### Inside a container, docker sandbox fails

Correct — there is no Docker socket in there, and mounting one would be a far
larger hole than it closes. Use `subprocess`; the container is the boundary.

### Generated code times out

Default 60s. The program is doing too much, or looping. `max_rpc_calls` (200)
also caps it. Ask for a narrower query rather than raising the timeout.

### "Importing 'os' is not allowed in the sandbox"

By design. The allow-list is 24 stdlib modules and contains no filesystem,
process, or network access. Everything Telegram-related goes through `tg`. See
[sandboxing](sandboxing.md).

---

## Storage

### `Database schema version 2 is newer than this build supports`

You downgraded. Upgrade tgagent again, or point at a different database. The
migration path is forward-only, deliberately — the alternative is corruption.

### `database is locked`

Two processes sharing one `TGAGENT_DATA_DIR`. Give each its own.

### Where is my data?

- Linux `~/.local/share/tgagent`
- macOS `~/Library/Application Support/tgagent`
- Windows `%APPDATA%\tgagent`

`tgagent config show` prints the resolved paths.

---

## Performance

### Media transfers are slow

```bash
pip install "tgagent[speedups]"     # cryptg: Rust AES-IGE
```

### Startup is slow the first time

The API index is built by reflecting over Telethon (~0.7s) and then cached. It
rebuilds only after a Telethon upgrade.

### Runs are expensive

- Nudge it toward `python` for bulk work — one turn instead of many.
- Search rather than paginate.
- Lower `max_tool_result_chars`.
- Lower `llm.effort` where supported.

---

## Reporting a bug

Include `tgagent version`, the OS, the provider and model, the sandbox backend,
and DEBUG logs. **Never paste your api_hash, session file, or LLM key** — logs
are redacted, but check anyway.

Security issues go through [private disclosure](../.github/SECURITY.md), never a
public issue.
