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

Something has to actually run the tasks. Both of these do:

```bash
tgagent listen          # commands from Telegram, and the scheduler with them
tgagent serve           # the scheduler alone
```

`listen` runs a scheduler by default, because a listener that accepts *"every
morning at eight"* and then silently never runs it is a trap — the task is saved,
the agent reports success, and nothing happens. Pass `--no-scheduler` if you
deliberately run the two as separate processes.

If you do split them, note what the running one knows: `schedule_create` says
`nothing_will_run_it` when it is called in a process with no scheduler, and
`agent ping` reports the scheduler's state and the next due task:

```
you    agent ping
bot    🏓 pong
       …
       scheduler: `running` · 3 task(s) · next in 12m 04s
```

```
       ⚠️ 3 enabled task(s), but the scheduler is OFF — none of them will run.
```

For a daemon see [deployment](deployment.md); `./hermes deploy` installs a
systemd service that runs both.

## Unattended semantics — read this

Scheduled runs pass `interactive=False`. **Nobody can answer a confirmation
prompt**, so every `confirm` becomes `non_interactive_decision` — `deny` by
default.

In practice a scheduled task can read, search, and download freely, and cannot
send, edit, forward, or delete unless you granted that explicitly. The system
prompt tells the agent it is unattended, so it plans accordingly and reports what
it could not do rather than failing repeatedly.

There are two ways to give a task more than that, and they are for different
situations.

### Granting a task what it needs, from the chat

A standing request usually arrives while you are there to authorise it — *"put
the time in my name every minute"* — so that is when you get asked:

```
you    agent put the time in my name every minute
bot    ⚠️ Confirmation needed (account_security)
       Operation : account.UpdateProfile
       Target    : task/clock-name
       Details   : The scheduled task 'clock-name' (every 1m) needs
                   account.UpdateProfile, and its runs have nobody to
                   confirm with. Granting it lets that task — and only
                   that task — perform this operation unattended until
                   you delete it.
       Reply yes to allow or no to refuse.
you    yes
bot    Set up: "clock-name", every 60s. Granted account.UpdateProfile
       to this task only. Delete the task to end it.
```

The agent works out what a task will need and names it in `schedule_create`'s
`needs` argument; the tool checks each operation against the policy *as an
unattended run will see it* and asks about the ones that would be refused. Your
`yes` is recorded on the task row and applied only to that task's runs:

```bash
tgagent tasks list        # the Granted column shows exactly this
```

Four limits make a grant narrower than a policy change, and all four matter:

| | |
| --- | --- |
| **Scoped to the task** | Applied around that task's runs only, through a `ContextVar`, so a chat-initiated run happening at the same time is unaffected. |
| **Lifts the decision, nothing else** | `read_only_mode`, `max_outbound_per_run`, and the chat allow/denylists are about blast radius rather than about this operation, and a grant does not touch them. |
| **Cannot lift what you forbade** | An explicit `method_overrides: X: deny` is a decision you made; a tier default is the absence of one. Only the latter can be granted. |
| **Cannot reach the account itself** | Nothing that can lock you out or move your credentials — password, 2FA, sessions, log-out, username, account deletion — is grantable from a chat, whatever you answer. |

The grant is visible in `tgagent tasks list`, in `schedule_list`, in the task
row, and in the reason attached to every audit entry it permits.

### Granting in the policy file

For anything a chat grant will not do — the account-security operations above, a
task that must send into exactly one chat, or a rule you want under version
control — the policy file is still the mechanism:

```yaml
method_overrides:
  messages.SendMessage: allow
chat_allowlist: ["@my_notes_bot"]      # and only there
max_outbound_per_run: 3
```

That is a deliberate, reviewable decision — which is why it lives in a file
rather than a flag. It applies to *every* run, which is exactly the difference
from a task grant, and it needs a restart to take effect.

### If you skip both

The task is still created, and it tells you what will happen:

```json
{"created": "clock-name", "schedule": "every 1m",
 "will_fail_every_run": [{"method": "account.UpdateProfile",
   "risk": "account_security", "decision": "deny",
   "policy_fix": "method_overrides:\n  account.UpdateProfile: allow"}]}
```

A task refused on every run is the worst outcome available here — 1,440 silent
failures a day, into a log nobody reads — so it is reported at setup, while
somebody is still there to read it.

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
