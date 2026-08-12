# tgagent

An autonomous AI agent that operates a **real Telegram user account** over
MTProto — not a bot.

You talk to it in natural language. It decomposes the request into Telegram
operations, executes them under a permission policy you control, and reports
back with evidence.

```console
$ tgagent run "read my conversation with Alex from January and summarise what we discussed"
$ tgagent run "find every message where I promised someone I'd do something"
$ tgagent run "search all my chats from last month for anything about the VPS migration"
$ tgagent chat
```

---

## Why a user account, not a bot

A Telegram bot cannot list your dialogs, cannot search your history, and cannot
see messages it was not addressed in. Almost nothing on the list above is
possible through the Bot API. tgagent authenticates as **you**, over MTProto,
using [Telethon](https://docs.telethon.dev) — which is also why the security
model below is the largest part of this project.

## What makes it work

**The whole Telegram API, not a hand-picked subset.** Telethon exposes ~824
generated request classes. Enumerating them as LLM tools is impossible; picking
twenty and calling it done is a toy. Instead there are three tiers:

| Tier | What it is | When it is used |
|---|---|---|
| ~13 curated `telegram_*` tools | Token-cheap, paginated, hand-written schemas | The common 90% |
| `telegram_api_search` | Full-text search over an index built by **reflecting over the installed Telethon** — always accurate, zero prompt cost | Discovering the other 810 methods |
| `python` (sandboxed) + `telegram_invoke` | Arbitrary composition against `tg.*` and `tg.invoke_raw(...)` | Loops, filtering, anything multi-step |

**Generated code holds no capability.** This is the design decision everything
else rests on. The sandbox process gets **no Telegram client, no credentials, no
session file, and no network** — only a proxy whose every call is marshalled over
a pipe to the host, where it is classified, authorised, confirmed, rate-limited,
and audited exactly like a curated tool call.

```
   sandbox (untrusted)                      host (trusted)
   ────────────────────                     ──────────────
   tg.get_messages(...)  ──── JSON pipe ──▶  classify → authorise → confirm
   no client                                 → execute → audit → serialise
   no keys, no socket    ◀───────────────    safe JSON only
```

An escape from the sandbox yields a process that can *ask* the gateway for
things — which is exactly what a tool can already do, and equally policed.

**Large histories are assumed, not accommodated.** Nothing loads a whole
conversation into context. Filtering is pushed to Telegram's servers, access is
cursor-paginated with hard caps, results are compact projections, and bulk
scanning happens inside the sandbox so 5,000 messages become 12 results in one
turn instead of fifty.

---

## Security in one page

Operating someone's real account means the interesting questions are all about
what the agent *cannot* do.

**Every operation is classified and policed.** Reads run freely; sends, edits,
forwards, joins, and deletes need your explicit confirmation; 2FA, sessions, and
privacy settings are denied outright. An **unrecognised** method is treated as
destructive, so a future Telethon release cannot introduce something that
executes without a decision. Policy lives in a YAML file you own.

**Telegram content is data, never instructions.** Message text, filenames, chat
titles, and usernames arrive fenced in a tag carrying a random per-process token,
so content cannot forge its way out into instruction context. A heuristic scanner
annotates likely injection attempts. But the load-bearing control is the
permission engine: an injection that completely succeeds can still only *ask*,
and every consequential action is gated in code.

**Nothing consequential happens silently.** Every Telegram call — allowed,
denied, or failed, from a tool or from generated code — lands in an audit log
with its risk tier, decision, target, and origin. `tgagent audit` reads it back.

Full detail: [security model](docs/security.md) · [threat model](docs/threat-model.md) ·
[prompt injection](docs/prompt-injection.md) · [sandboxing](docs/sandboxing.md) ·
[permissions](docs/permissions.md)

---

## Quick start

```bash
pip install "tgagent[anthropic]"          # or [openai], or [all]

cp .env.example .env                      # then fill in the values
tgagent config check                      # tells you what is still missing
tgagent login                             # phone → code → 2FA password
tgagent run "list my 10 most recent chats and tell me which have unread messages"
```

You need two things:

1. **Telegram API credentials** — `api_id` and `api_hash` from
   <https://my.telegram.org/apps>. See [Telegram setup](docs/telegram-setup.md).
2. **An LLM provider** — Anthropic and any OpenAI-compatible endpoint
   (OpenRouter, Groq, vLLM, Ollama, LM Studio) are built in. See
   [LLM providers](docs/llm-providers.md).

> The `.session` file created by `tgagent login` is an **authenticated
> credential**. Anyone holding it can read and send as your account without your
> phone, password, or 2FA. It is git-ignored; keep it that way.

---

## What it can do

Everything Telethon and Telegram support is reachable. In practice:

- **Read** dialogs, history, threads, replies, reactions, participants, drafts,
  scheduled messages, admin logs.
- **Search** within a chat or globally, with date ranges, sender filters, and
  media-type filters — all server-side.
- **Act**, subject to policy: send, edit, forward, delete, pin, mark read, react,
  join, leave, manage chats and permissions.
- **Media**: download with size, MIME, and extension validation, into a per-run
  directory that is reaped on a retention schedule.
- **Remember** durable facts across runs, and **schedule** recurring work
  ("every morning, review my unread messages").

What it deliberately will not do, and what Telegram itself will not let it do, is
in [known limitations](docs/limitations.md).

---

## Documentation

| | |
|---|---|
| **Start here** | [Installation](docs/installation.md) · [Telegram setup](docs/telegram-setup.md) · [Authentication](docs/authentication.md) · [Usage](docs/usage.md) |
| **Configure** | [Configuration](docs/configuration.md) · [LLM providers](docs/llm-providers.md) · [Permissions](docs/permissions.md) |
| **Understand** | [Architecture](docs/architecture.md) · [Agent runtime](docs/agent-runtime.md) · [Tool architecture](docs/tool-architecture.md) · [Telegram integration](docs/telegram-integration.md) · [Memory](docs/memory.md) · [Scheduling](docs/scheduling.md) |
| **Security** | [Security model](docs/security.md) · [Threat model](docs/threat-model.md) · [Prompt injection](docs/prompt-injection.md) · [Sandboxing](docs/sandboxing.md) · [Privacy](docs/privacy.md) |
| **Operate** | [Deployment](docs/deployment.md) · [Troubleshooting](docs/troubleshooting.md) · [Limitations](docs/limitations.md) |
| **Develop** | [Development](docs/development.md) · [Testing](docs/testing.md) · [CI/CD](docs/ci-cd.md) · [Extending](docs/extending.md) · [Decisions](docs/decisions/) · [Contributing](CONTRIBUTING.md) |

## Requirements

Python 3.11+ · a Telegram account · an LLM provider key. Docker is optional but
recommended for the strongest sandbox.

## License

MIT — see [LICENSE](LICENSE).

## Responsible use

This automates a real person's account. Automating **your own** account is what
it is for. Using it to operate someone else's account without their knowledge,
to send bulk unsolicited messages, or to scrape other people's data is both a
Telegram terms-of-service violation and, in many places, illegal. The permission
system exists to protect the account owner, not to make misuse convenient.
