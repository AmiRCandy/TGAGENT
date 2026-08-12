# Permission system

Implemented in [`security/permissions.py`](../src/tgagent/security/permissions.py)
and enforced in [`telegram/gateway.py`](../src/tgagent/telegram/gateway.py). It is
code, not documentation: nothing reaches Telegram without passing through it.

## Risk tiers

| Tier | Meaning | Examples |
|---|---|---|
| `read_only` | Reads state; nobody else can tell | `get_messages`, `messages.Search`, `get_dialogs`, `get_participants` |
| `reversible` | Trivially undoable, nobody is notified | `messages.ReadHistory`, `download_media`, `messages.SaveDraft` |
| `externally_visible` | **Other people see it** | `send_message`, `edit_message`, `forward_messages`, `channels.JoinChannel`, `messages.SendReaction`, uploads |
| `destructive` | Destroys data | `delete_messages`, `messages.DeleteHistory`, `channels.EditBanned`, `channels.LeaveChannel`, `contacts.Block` |
| `account_security` | The account's security posture | `account.UpdatePasswordSettings`, `auth.LogOut`, `account.ResetAuthorization`, `account.UpdatePrivacy` |

Tiers are ordered, so a policy can express "confirm anything at
`externally_visible` or above".

### Classification

Method names arrive in two shapes — raw TL (`messages.SendMessage`) and Telethon
friendly (`send_message`) — and both are normalised (lowercased, `Request` suffix
stripped) before matching explicit tables. A policy written either way governs
calls made either way; capitalisation cannot be used to dodge a rule.

That equivalence applies to `method_overrides` too, and it is worth being precise
about, because the gateway exposes both routes to the same operation:

* **Separators and case are ignored.** `send_message`, `sendMessage` and
  `messages.SendMessage` are one override. Only separators are folded, never
  letters, so `deletehistory` and `deletemessages` stay distinct.
* **A namespace is compared only when both sides carry one.** `send_message`
  (which has none) still governs `messages.SendMessage`, but
  `channels.DeleteMessages: allow` does *not* leak onto `messages.DeleteMessages`
  — those are two different operations, and widening a grant is the one direction
  of sloppiness that fails open.
* **Overlapping spellings that disagree resolve to the strictest.** Writing
  `send_message: allow` next to `messages.SendMessage: deny` denies. Dict
  iteration order must not decide a security question.
* **A friendly wrapper is not a re-spelling of the requests it issues.**
  `delete_dialog` issues `messages.DeleteHistory`, `DeleteChatUser`, or
  `channels.LeaveChannel` depending on the peer, so no normalisation can bridge
  them. A small curated table connects the two, and an override reached through
  it may only **tighten** a verdict, never loosen it: the table is hand-written
  and non-exhaustive, so a missing pair falls back to the tier default rather
  than to `allow`.

Because an override no longer has to *look* like the call it governs, ask the
engine rather than reading the YAML — `tgagent config policy <method>` reports the
line that actually applies.

**The fallback is the important part.** An unrecognised method that does not look
like a read is classified `destructive`:

```python
classify("messages.SomeBrandNewThing")  # → DESTRUCTIVE
classify("obliterate_everything")  # → DESTRUCTIVE
classify("messages.GetSomethingNew")  # → READ_ONLY  (read-shaped prefix)
```

A future Telethon release cannot introduce a method that silently executes.

Check any method:

```console
$ tgagent config policy messages.DeleteHistory
╭──────────── Policy explanation ────────────╮
│ Method   : messages.DeleteHistory          │
│ Risk tier: destructive                     │
│ Decision : confirm                         │
│ Override : no (risk-tier default)          │
╰────────────────────────────────────────────╯

$ tgagent config policy messages.SendMessage      # policy says `send_message: deny`
╭──────────── Policy explanation ────────────╮
│ Method   : messages.SendMessage            │
│ Risk tier: externally_visible              │
│ Decision : deny                            │
│ Override : yes — send_message              │
╰────────────────────────────────────────────╯
```

The second panel is why this command asks the engine instead of looking the method
up in the policy dictionary: the override that governs the call is not spelled
like it.

## Decisions

`allow` · `confirm` · `deny`. Defaults:

```yaml
read_only: allow
reversible: allow
externally_visible: confirm
destructive: confirm
account_security: deny
```

A **denial is not a crash**. It is returned to the model as a tool result, so the
agent adapts and reports rather than the run dying. The system prompt explicitly
tells it not to route around a refusal by finding a different method that does
the same thing.

## The policy file

Copy [`policy.example.yaml`](../policy.example.yaml) and point at it:

```bash
export TGAGENT_PERMISSIONS__POLICY_FILE=./policy.yaml
```

```yaml
defaults:
  read_only: allow
  reversible: allow
  externally_visible: confirm
  destructive: confirm
  account_security: deny

method_overrides:
  messages.DeleteHistory: deny       # no case where an agent should purge history
  channels.LeaveChannel: deny        # loses access to the history
  messages.ReadHistory: allow

chat_allowlist: ["@my_notes_bot"]    # writes confined to these
chat_denylist:  ["@company_announcements"]

read_only_mode: false
non_interactive_decision: deny
confirmation_timeout: 300
max_outbound_per_run: 20
```

**Unknown keys are a hard error.** A typo in a security policy that silently does
nothing is exactly the failure worth preventing:

```
Unknown key(s) in policy ./policy.yaml: read_only_mod.
Valid keys: chat_allowlist, chat_denylist, confirmation_timeout, defaults, …
```

A partial `defaults` mapping is merged with the conservative baseline, so
tightening one tier cannot leave the others undefined.

## Beyond per-call decisions

Blast-radius limits, independent of whether any individual call was approved:

| Control | Default | Purpose |
|---|---|---|
| `max_outbound_per_run` | 20 | Even fully approved, one run cannot message a hundred people |
| `min_seconds_between_writes` | 1.0s | Spacing so a looping agent cannot trip Telegram's spam heuristics |
| `chat_allowlist` / `chat_denylist` | — | Writes confined to (or excluded from) named chats. Reads unaffected |
| `read_only_mode` | false | Global kill switch |

Chat lists normalise `@`, case, and `t.me` links, so `@Work`, `work`, `t.me/work`
and `https://www.t.me/work/42` are one thing. With **either** list configured, a
write whose target the engine cannot name is denied rather than assumed safe —
`contacts.Block(id="@x")` names its peer in an argument the engine does not
recognise, and used to walk straight past the denylist.

> **A chat list matches how the call names the chat, not the chat itself.**
> Resolving a username to an id needs a network round trip, and this engine is
> synchronous by design. So `@company_announcements` does **not** match
> `-1001234567890`, nor an invite link like `t.me/+AbCdEf`, nor `t.me/c/123/7`.
> List a chat under every reference an agent might plausibly use — username *and*
> numeric id. `max_outbound_per_run` and `confirm` are what bound the case you
> forgot.

## Confirmation

When policy says confirm, a `ConfirmationProvider` asks. Which one depends on the
interface:

| Provider | Used by |
|---|---|
| `CallbackConfirmation` | CLI — prompts on the terminal, with a timeout |
| `AutoDenyConfirmation` | Default for unattended execution |
| `AutoApproveConfirmation` | Tests, and `--yes`. Logs loudly every time |

Every provider must honour a timeout; a prompt that hangs would wedge the loop,
so silence resolves to **declined**.

Approving a non-destructive operation offers "remember for this run", which
covers a task that legitimately sends several messages. Destructive operations
never offer it.

### Unattended runs

Scheduled tasks have nobody to ask, so `confirm` becomes
`non_interactive_decision` — **deny** by default. The system prompt tells the
agent it is unattended so it plans accordingly and reports what it could not do.

If a scheduled task genuinely needs to send, grant that explicitly and narrowly:

```yaml
method_overrides:
  messages.SendMessage: allow
chat_allowlist: ["@my_notes_bot"]
max_outbound_per_run: 3
```

That is a deliberate, reviewable decision — which is the point of it living in a
file rather than a flag.

## How it is wired

```mermaid
flowchart LR
    T[Curated tool] --> GW
    S[Sandboxed code] -->|RPC| GW
    SC[Scheduler] --> GW
    GW[TelegramGateway.call] --> C{classify}
    C --> P{policy lookup}
    P -->|allow| L[limits + throttle]
    P -->|confirm| CF{ConfirmationProvider}
    P -->|deny| D[PermissionDenied]
    CF -->|yes| L
    CF -->|no| D
    L --> EX[execute]
    EX --> A[(audit log)]
    D --> A
```

Note that all three entry points converge *before* classification. There is no
path that reaches `execute` without passing `policy lookup`.

## Testing

[`tests/test_permissions.py`](../tests/test_permissions.py) covers classification
of ~40 methods across all five tiers, the unknown-method fallback, case and
suffix insensitivity, every decision path, chat lists, outbound budgets, and
non-interactive fallback. [`tests/test_gateway.py`](../tests/test_gateway.py)
proves enforcement actually happens at the choke point, and
[`tests/test_integration.py`](../tests/test_integration.py) proves generated code
cannot route around it.
