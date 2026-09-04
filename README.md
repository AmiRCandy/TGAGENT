<div align="center">

# tgagent

**Your Telegram account, with an agent inside it.**

Not a bot. It signs in as *you* over MTProto, reads what you can read, acts under
a permission policy you control, and lives in the chats you already use.

[![CI](https://github.com/AmiRCandy/tgagent/actions/workflows/ci.yml/badge.svg)](https://github.com/AmiRCandy/tgagent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Quick start](#quick-start) · [What it can do](#what-it-can-do) ·
[Security](#security-in-one-page) · [Plugins](#plugins) · [Docs](#documentation)

</div>

---

```
you    agent what did I miss in here today?
bot    Three threads moved. Alex needs the migration date by Friday, Sara
       shared the invoice you asked for, and the deploy thread resolved itself.

you    agent every morning at 8, tell me what needs a reply
bot    Set up: "morning-review", 08:00 Europe/London, first run tomorrow.

you    agent reply to Sina while I'm on the flight — short, like I write to him
bot    Answering @sina for 4 hours, up to 20 replies. I read your last 50
       messages with him for tone. `agent unwatch` stops it.

you    agent ping
bot    🏓 pong · send round-trip 142 ms · last heard from Telegram 12s ago
       scheduler: running · 3 tasks · next in 12m 04s
```

You never leave Telegram. The terminal is for setup.

---

## Why a user account, not a bot

A bot cannot list your dialogs, cannot search your history, and cannot see
messages it was not addressed in. Almost nothing above is possible through the
Bot API. tgagent authenticates as **you**, using
[Telethon](https://docs.telethon.dev) — which is also why the security model
below is the largest part of this project.

## Quick start

```bash
git clone https://github.com/AmiRCandy/tgagent && cd tgagent
./install.sh                 # credentials, provider, sign-in
./hermes listen              # now talk to it from Telegram
```

`install.sh` builds a venv, asks for your Telegram `api_id`/`api_hash` and a
model provider, writes them to a `600`-mode `.env`, and offers to sign in. Run it
again whenever — it keeps what is already there.

By hand, if you prefer:

```bash
pip install "tgagent[anthropic]"     # or [openai], or [all]
cp .env.example .env                 # fill in the values
tgagent config check                 # says what is still missing
tgagent login                        # phone → code → 2FA
tgagent run "list my 10 most recent chats and which have unread messages"
```

You need two things: **Telegram API credentials** from
<https://my.telegram.org/apps> ([setup](docs/telegram-setup.md)), and **an LLM
provider** — Anthropic, or any OpenAI-compatible endpoint including OpenRouter,
Groq, vLLM and Ollama ([providers](docs/llm-providers.md)).

To keep it running:

```bash
./hermes deploy              # systemd user service, restarts on failure
./hermes status              # is it up, is lingering on, is the unit current
./hermes logs
```

> [!WARNING]
> The `.session` file created by `tgagent login` is an **authenticated
> credential**. Anyone holding it can read and send as your account without your
> phone, password, or 2FA. It is git-ignored; keep it that way.

## What it can do

**The whole Telegram API, not a hand-picked subset.** Telethon exposes ~824
request classes. Enumerating them as tools is impossible; picking twenty and
calling it done is a toy. So there are three tiers:

| Tier | What it is | When it is used |
|---|---|---|
| ~13 curated `telegram_*` tools | Token-cheap, paginated, hand-written schemas | The common 90% |
| `telegram_api_search` | Full-text search over an index built by reflecting over the *installed* Telethon — always accurate, zero prompt cost | Discovering the other 810 methods |
| `python` (sandboxed) + `telegram_invoke` | Arbitrary composition against `tg.*` | Loops, filtering, anything multi-step |

In practice:

- **Read** dialogs, history, threads, replies, reactions, participants, drafts,
  scheduled messages, admin logs.
- **Search** in a chat or globally, with date, sender and media filters — all
  server-side.
- **Act**, subject to policy: send, edit, forward, delete, pin, mark read, react.
- **Remember** across runs, so "the team channel" keeps meaning the same thing.
- **Keep doing things** — "every morning at 8", "every minute" — as scheduled
  tasks that survive restarts. See [scheduling](docs/scheduling.md).
- **Answer people for you** while you are away, in your own voice, bounded by an
  expiry and a reply budget. See [answering for you](docs/autoreply.md).
- **Reach past Telegram** through [plugins](#plugins) — web search, downloads,
  whatever you write.

**Large histories are assumed, not accommodated.** Nothing loads a whole
conversation into context: filtering is pushed to Telegram's servers, access is
cursor-paginated with hard caps, and bulk scanning happens inside the sandbox so
5,000 messages become 12 results in one turn instead of fifty.

## Security in one page

Operating someone's real account means the interesting questions are all about
what the agent *cannot* do.

**Every operation is classified and policed.** Reads run freely; sends, edits,
forwards, joins and deletes need your confirmation; 2FA, sessions and privacy
settings are denied outright. An **unrecognised** method is treated as
destructive, so a future Telethon release cannot introduce something that
executes without a decision. Policy lives in a YAML file you own — and you can
change it from a chat:

```
you    agent policy add send_message
bot    ✅ `send_message` → **allow** (risk: externally_visible)
       In force now, and after a restart.
```

**Generated code holds no capability.** This is the decision everything else
rests on. The sandbox process gets **no Telegram client, no credentials, no
session file, and no network** — only a proxy whose every call is marshalled over
a pipe to the host, where it is classified, authorised, confirmed, rate-limited
and audited exactly like a curated tool call.

```
   sandbox (untrusted)                      host (trusted)
   ────────────────────                     ──────────────
   tg.get_messages(...)  ──── JSON pipe ──▶  classify → authorise → confirm
   no client                                 → execute → audit → serialise
   no keys, no socket    ◀───────────────    safe JSON only
```

An escape from the sandbox yields a process that can *ask* the gateway for
things — which is what a tool can already do, and equally policed.

**Telegram content is data, never instructions.** Message text, filenames, chat
titles and usernames arrive fenced in a tag carrying a random per-process token,
so content cannot forge its way into instruction context. A scanner annotates
likely injection attempts. But the load-bearing control is the permission engine:
an injection that completely succeeds can still only *ask*.

**Nothing consequential happens silently.** Every Telegram call — allowed,
denied or failed, from a tool or from generated code — lands in an audit log with
its risk tier, decision, target and origin. `tgagent audit` reads it back.

Full detail: [security model](docs/security.md) ·
[threat model](docs/threat-model.md) ·
[prompt injection](docs/prompt-injection.md) · [sandboxing](docs/sandboxing.md) ·
[permissions](docs/permissions.md)

## Talk to it from Telegram

With `tgagent listen` running, any chat becomes the prompt. The instruction
arrives with its own context — which chat, who sent it, what it replied to — so
"here", "this" and "them" resolve without you spelling them out.

```
you    agent tell alex I'm running late          (replying to his message)
bot    ⚠️ Confirmation needed (externally_visible)
       messages.SendMessage to @alex — "Running about 15 minutes late, sorry!"
       Reply yes to allow or no to refuse.
you    yes
bot    Sent.
```

A slow run says so while it is slow: the command is acknowledged at once and
that one message is edited every few seconds with what the agent is doing, until
the answer replaces it. Only *your* messages count as commands; everyone else's
text, including a message you quote, is fenced as untrusted data.

`agent help` is the whole surface, with an example of each part. More in
[Telegram control](docs/telegram-control.md).

## Plugins

Extra tools, installed from a git URL and managed from a chat:

```
you    agent plugin add someone/tgagent-weather
you    agent plugin set web-search api_key BSA…
you    agent plugin off youtube
you    agent plugin list
```

Two ship with it: **web-search** (`web_search`, `web_fetch`) and **youtube**
(`youtube_info`, `youtube_download`). Writing one is a `plugin.toml` and a
function returning tools.

A plugin's code runs **inside the agent**, with the account's credentials — it is
not the sandbox — so installing one is a decision of the same size as installing
tgagent. What the loader guarantees anyway: output fenced as untrusted, every
call audited, no shadowing a built-in tool name, and a broken plugin that never
stops the agent from starting.

Both halves of the story — using and writing — are in
[plugins](docs/plugins.md).

## Documentation

| | |
|---|---|
| **Start here** | [Installation](docs/installation.md) · [Telegram setup](docs/telegram-setup.md) · [Authentication](docs/authentication.md) · [Usage](docs/usage.md) · [Telegram control](docs/telegram-control.md) |
| **Configure** | [Configuration](docs/configuration.md) · [LLM providers](docs/llm-providers.md) · [Permissions](docs/permissions.md) |
| **Features** | [Scheduling](docs/scheduling.md) · [Answering for you](docs/autoreply.md) · [Plugins](docs/plugins.md) · [Memory](docs/memory.md) |
| **Understand** | [Architecture](docs/architecture.md) · [Agent runtime](docs/agent-runtime.md) · [Tool architecture](docs/tool-architecture.md) · [Telegram integration](docs/telegram-integration.md) |
| **Security** | [Security model](docs/security.md) · [Threat model](docs/threat-model.md) · [Prompt injection](docs/prompt-injection.md) · [Sandboxing](docs/sandboxing.md) · [Privacy](docs/privacy.md) |
| **Operate** | [Deployment](docs/deployment.md) · [Troubleshooting](docs/troubleshooting.md) |
| **Develop** | [Testing](docs/testing.md) · [CI/CD](docs/ci-cd.md) · [Decisions](docs/decisions/) |

## Requirements

Python 3.11+, a Telegram account, and an LLM provider key. Runs on Linux, macOS
and Windows; deployment targets systemd or Docker. Roughly 150 MB of RAM idle —
a 1 GB VPS is plenty.

## License

MIT. See [LICENSE](LICENSE).

## Responsible use

This operates a real account as a real person. Do not use it to spam, to
impersonate anyone but yourself, to evade a block, or to collect data on people
who have not agreed to it. Some jurisdictions require automated correspondence to
be disclosed — [autoreply](docs/autoreply.md) has a setting for that, and whether
you need it is your call. Telegram's terms apply to everything the agent does,
because to Telegram it is all just you.
