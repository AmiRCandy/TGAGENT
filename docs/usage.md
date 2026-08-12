# Running the agent

## One-shot

```bash
tgagent run "read my conversation with Alex from January and summarise it"
```

Flags:

| Flag | Effect |
|---|---|
| `-v`, `--verbose` | Show tool arguments as they are called |
| `-c`, `--conversation ID` | Continue an earlier conversation |
| `--read-only` | Block every write for this run |
| `-y`, `--yes` | Auto-approve confirmations. **Dangerous** — the policy file becomes your only protection |

## Interactive

```bash
tgagent chat
```

Context carries across turns within a session. `Ctrl-C` cancels the current run
without leaving the session; `Ctrl-D` or `/exit` leaves.

## Scheduler

```bash
tgagent serve
```

Runs due tasks in the foreground until interrupted. Scheduled runs are
**unattended**: anything that would need a confirmation is decided by
`non_interactive_decision` (deny, by default). See [scheduling](scheduling.md).

## Other commands

```bash
tgagent login / logout / whoami        # authentication
tgagent config check                   # what is configured, what is missing
tgagent config show [--show-secrets]   # effective configuration
tgagent config policy [METHOD]         # policy, or how one method is classified
tgagent tasks list / add / remove / run
tgagent audit [-n 50] [--run ID]       # what actually happened
tgagent api "search messages by date"  # the same API index the agent uses
tgagent sandbox                        # what the sandbox isolates, with live probes
tgagent version
```

## Writing good requests

The agent is capable but not telepathic. Requests that work well are specific
about **scope**, **where to look**, and **what "done" means**.

**Be specific about scope.** "Summarise my chats" is unbounded; the agent will
either ask or pick something arbitrary.

```
✗ summarise my chats
✓ summarise my conversation with @alex from 1 January to 31 January
```

**Say where to look when you know.** It saves a discovery round trip.

```
✗ find the server credentials
✓ search my chat with @ops for messages mentioning credentials or ssh
```

**Ask for the shape of the answer.**

```
✓ list every message where I promised to do something, as a table of
  date, chat, what I promised, and whether it looks done
```

**Multi-step requests are fine — that is the point.**

```
review my conversations with Alex over the last month, find everything about
project X, download any relevant files, and give me a structured summary
```

The agent will resolve Alex, work out the date range, search server-side, filter
the results, inspect media, download what matters, and write the report. Expect
a confirmation prompt only if something crosses a write boundary.

## Confirmations

When policy says confirm, you get:

```
╭──────── Confirmation required ────────╮
│ Operation : send_message              │
│ Risk      : externally_visible        │
│ Target    : @alex (id 123456789)      │
│ Policy    : Policy requires confirmation for externally_visible operations. │
│ Details   : [externally_visible] send_message → @alex (message='On my way') │
╰───────────────────────────────────────╯
Allow this operation? [y/N]:
```

Answering yes to a non-destructive operation offers "allow this operation type
for the rest of the run" — useful when a task legitimately sends several
messages. Destructive operations never offer it; each deletion is confirmed
individually.

Declining is not an error. The refusal is reported to the model, which adapts and
tells you what it could not do.

## Understanding what happened

The run summary line:

```
4 step(s) · 6 tool call(s) · 18432 tokens · 12.3s
conversation: 8f3a1c…
```

`--verbose` shows each tool call with its arguments. For the security-relevant
record — which is the authoritative one — use the audit log:

```console
$ tgagent audit -n 20
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━┓
┃ When                ┃ Method         ┃ Risk       ┃ Decision ┃ Target ┃ Origin ┃ OK ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━┩
│ 2026-08-12 09:14:02 │ get_dialogs    │ read_only  │ allow    │        │ tool   │ ✓  │
│ 2026-08-12 09:14:05 │ messages.Search│ read_only  │ allow    │ @alex  │ sandbox│ ✓  │
│ 2026-08-12 09:14:31 │ send_message   │ externally…│ allow    │ @alex  │ tool   │ ✓  │
└─────────────────────┴────────────────┴────────────┴──────────┴────────┴────────┴────┘
```

`origin` distinguishes curated tool calls from calls made by generated code.

## Cancelling

`Ctrl-C` during a run sets the cancellation flag. The runtime stops before the
next step and before any queued tool, and reports what it had. An operation
already in flight at Telegram completes — the agent cannot un-send a request that
has left.

## Conversations

Each run belongs to a conversation. Continue one with `-c`:

```bash
$ tgagent run "who did I talk to yesterday?"
conversation: 8f3a1c2b…

$ tgagent run "summarise the third one" -c 8f3a1c2b…
```

Prior turns are reloaded (up to `agent.history_limit`) and compacted when they
grow past the context budget.

## Read-only mode

While you are building confidence:

```bash
tgagent run --read-only "…"                          # one run
export TGAGENT_PERMISSIONS__READ_ONLY_MODE=true      # everything
```

Every write is refused, and the system prompt tells the agent so it reports
rather than repeatedly attempting.
