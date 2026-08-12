# Scheduling

Recurring agent runs: *"every morning, review my unread messages and tell me if
anything important happened."*

## Creating tasks

From the CLI:

```bash
tgagent tasks add morning-review \
  "Review unread messages since yesterday and summarise anything needing a reply." \
  --cron "0 8 * * *" --timezone Europe/London

tgagent tasks add hourly-watch "Check @ops for new alerts." --every 3600
tgagent tasks add one-off "Remind me about the deploy." --once 2026-09-01T08:00:00Z

tgagent tasks list
tgagent tasks run morning-review     # run it now, with unattended semantics
tgagent tasks remove morning-review
```

Or by asking:

```
> every morning at 8, review my unread messages and flag anything needing a reply
```

The agent uses `schedule_create`, and `schedule_list` first so it does not
duplicate an existing task.

## Running the scheduler

```bash
tgagent serve
```

Foreground until interrupted. For a daemon see [deployment](deployment.md).

## Unattended semantics — read this

Scheduled runs pass `interactive=False`. **Nobody can answer a confirmation
prompt**, so every `confirm` becomes `non_interactive_decision` — `deny` by
default.

In practice a scheduled task can read, search, and download freely, and cannot
send, edit, forward, or delete unless you granted that explicitly. The system
prompt tells the agent it is unattended, so it plans accordingly and reports what
it could not do rather than failing repeatedly.

If a task genuinely needs to send, grant it narrowly in the policy file:

```yaml
method_overrides:
  messages.SendMessage: allow
chat_allowlist: ["@my_notes_bot"]      # and only there
max_outbound_per_run: 3
```

That is a deliberate, reviewable decision — which is why it lives in a file
rather than a flag.

## Schedule kinds

| Kind | Expression | Example |
|---|---|---|
| `cron` | 5-field cron + IANA timezone | `0 8 * * *`, `Europe/London` |
| `interval` | seconds (minimum 30) | `3600` |
| `once` | ISO-8601 timestamp | `2026-09-01T08:00:00Z` |

Cron is timezone-aware because "every morning at 8" means *local* 8 and must keep
meaning that across a DST transition. `tzdata` is a hard dependency so this works
identically on Windows and on slim Linux images, neither of which ships a system
tz database.

Invalid expressions are rejected at creation with an explanation:

```
'every morning' is not a valid cron expression. Use 5 fields:
minute hour day-of-month month day-of-week — e.g. '0 8 * * *' for 08:00 every day.
```

## How it behaves

**Tasks are data, not code.** A row is an id, a schedule, a prompt, and some
metadata. That is why they survive restarts and upgrades — and why they can be
inspected with `sqlite3`. APScheduler's persistent stores pickle callables, which
breaks across upgrades and is a deserialisation risk; see
[ADR 0003](decisions/0003-scheduler.md).

**Claiming is a database compare-and-swap**, so two processes pointed at the same
database cannot double-fire a task.

**Misfires are skipped, not stampeded.** A run more than `misfire_grace` (900s)
late is skipped and rescheduled. A laptop waking after a weekend does not run
three days of "daily summaries" back to back.

**Failures do not stop the schedule.** The error is recorded on the task row and
the next run is computed as normal.

**Startup reconciles.** A task whose `next_run_at` is null — because it was
created while the scheduler was down, or a process died mid-run — is given one
rather than being stranded.

**Shutdown drains.** In-flight tasks get up to 30 seconds to finish before being
cancelled.

## Inspecting

```console
$ tgagent tasks list
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━┓
┃ Name           ┃ Schedule             ┃ Enabled ┃ Next run         ┃ Last status ┃ Runs ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━┩
│ morning-review │ cron '0 8 * * *' (…) │ yes     │ 2026-08-13 07:00 │ succeeded   │ 12   │
└────────────────┴──────────────────────┴─────────┴──────────────────┴─────────────┴──────┘
```

What a task actually *did* is in the audit log:

```bash
tgagent audit -n 50
```

## Writing good task prompts

A scheduled prompt runs with no one to clarify with, so it must be
self-contained.

```
✗ check my messages
✓ Review unread messages received since yesterday. For each, note the chat, who
  sent it, and whether it needs a reply. Group by urgency. If nothing needs a
  reply, say so briefly.
```

Say what "nothing to report" looks like, or you will get a paragraph of
throat-clearing every morning.

Where does the output go? By default into the conversation record — read it with
`tgagent audit` or by querying the database. To have it delivered, have the task
message your own Saved Messages, and allow exactly that:

```yaml
method_overrides:
  messages.SendMessage: allow
chat_allowlist: ["me"]
```

## Multiple schedulers

Claiming makes concurrent schedulers safe against double-firing, but two
processes sharing one SQLite file is not a configuration to seek out. If you need
several agents, give each its own `TGAGENT_DATA_DIR`.
