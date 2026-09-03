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

## It stops a while after I log out of SSH

The journal shows an orderly stop, not a crash:

```
systemd[94497]: Stopping tgagent.service...
systemd[94497]: Stopped tgagent.service.
tgagent.service: Consumed 2.249s CPU time over 22min 36.554s wall clock time
```

and the same build under `nohup` runs for weeks.

**Lingering is off.** A `systemctl --user` service belongs to your *user
manager*, and systemd stops that when your last session ends — taking every
`--user` unit with it. `nohup` survives because those processes live in the
session scope instead, which `KillUserProcesses=no` leaves alone.

Two tells confirm it:

- the message is **Stopping**, not a failure or an exit code — nothing crashed;
- the `systemd[…]` PID differs between runs, because the user manager itself
  exited and came back at your next login.

Fix it once:

```bash
sudo loginctl enable-linger "$(id -un)"
./hermes deploy
```

`./hermes deploy` now enables lingering itself and refuses to install the service
if it cannot, and `./hermes status` reports the state — this failure looks so much
like a crash that it is worth checking first.

## It ran for half an hour and then stopped answering

The logs still say it is running, `systemctl --user status tgagent` is green, and
only a restart brings it back.

**What is happening.** A NAT or conntrack table dropped the idle MTProto flow
without sending an RST. The client still reports itself connected, Telethon's
`disconnected` future never resolves, and no update ever arrives again. Sending
keeps working — a write forces a fresh connection — so the process looks healthy
from the inside, from its own logs, and from anything that only checks the
socket. Fifteen to thirty minutes is the usual interval on a VPS.

**What now happens instead.** The client tracks when it last *heard* from
Telegram, counting any update at all. Quiet for longer than
`telegram.idle_probe_after` (5 minutes) and it asks Telegram a question with a
deadline, because a dead socket does not refuse a request — it swallows it. A
failed probe tears the connection down and rebuilds it, then calls `catch_up()`
for what was missed. After `telegram.max_recovery_attempts` consecutive
failures the process exits 1 so the service manager replaces it; `Restart=always`
in the unit file is what makes that work.

Ask it directly:

```
you    agent ping
bot    🏓 pong
       …
       last heard from Telegram: 12s ago · healthy
```

A large number there, or `stale, rebuilding`, is this fault. In the journal:

```bash
./hermes logs | grep -E "telegram.(probing|connection_stale|connection_rebuilt|giving_up)"
```

### Why it only happened under systemd

Two things, and the second is why `screen` and `nohup` looked fine:

1. A monitoring handler was registered as a plain function. Telethon *awaits*
   whatever a handler returns, so every single update raised
   `TypeError: 'NoneType' object can't be awaited` — one logged traceback per
   update. Fixed, with a test that asserts every handler we register is a
   coroutine function.
2. Under a service manager stdout is a pipe to the journal. A message repeating
   per-update writes faster than the journal drains it, and a blocking write from
   the event loop stalls the whole process: up, idle, answering nothing, with its
   own logs as the cause. Redirected to a file or a terminal the same build looks
   healthy. Identical messages are now capped at 20 per minute per
   `(logger, message)` — distinct messages are never suppressed, and the record at
   the cap says so.

The unit file also had `StartLimitIntervalSec` in `[Service]`, where systemd
ignores it and says so in the journal, leaving the restart loop unbounded. It is
in `[Unit]` now.

If you are on an older unit file, redeploy it — `Restart=on-failure` never fires
for a process that is hung rather than crashed:

```bash
./hermes deploy
```

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

### Telegram commands get no answer at all

Send `agent ping`. It is answered by the bridge itself — no model, no tokens — so
what comes back separates the possibilities:

- **Nothing.** The listener is not running, not signed in, or not watching that
  chat. Check `./hermes logs` for `control.listening`, and `allowed_chats` /
  `ignored_chats` if other chats do answer.
- **A pong with `runs in flight: 2`.** It is busy, not dead — `max_concurrent_runs`
  is reached and your command is queued behind the others.
- **A pong with `commands this minute: 6/6`.** The loop breaker has fired. That is
  logged at error level; something sent a burst of commands, and the log says what.
- **A pong that takes seconds, or a large `command reached me in`.** The host is
  overloaded, the network is bad, or its clock is wrong — the lag figure compares
  Telegram's timestamp against the local clock, so skew shows up here.

A pong with everything looking healthy while ordinary instructions still fail
points at the LLM rather than the bridge: try `tgagent models`, or `tgagent run`
from the terminal for the full error.

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
