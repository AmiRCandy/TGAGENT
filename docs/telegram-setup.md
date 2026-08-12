# Telegram API setup

## Getting `api_id` and `api_hash`

1. Go to <https://my.telegram.org/apps>.
2. Sign in with **the phone number of the account the agent will operate**. You
   will get a login code in Telegram itself.
3. Choose **API development tools**.
4. Fill in the form:
   - **App title** — anything, e.g. `tgagent`
   - **Short name** — anything, e.g. `tgagent`
   - **Platform** — Desktop
   - **URL / description** — optional
5. Submit. You get an **App api_id** (a number) and an **App api_hash** (32 hex
   characters).

Put them in `.env`:

```bash
TGAGENT_TELEGRAM__API_ID=1234567
TGAGENT_TELEGRAM__API_HASH=0123456789abcdef0123456789abcdef
TGAGENT_TELEGRAM__PHONE=+15551234567
```

### What these actually are

`api_id`/`api_hash` identify **the application**, not the account. They are the
same for every user of a given app. They are still secrets — anyone with them can
build a client that presents itself as yours, and Telegram bans keys that turn up
in public repositories.

They are **not** enough to access your account. Signing in additionally needs the
phone number, a login code sent to your Telegram, and your 2FA password if you
have one. That is [authentication](authentication.md).

> **Do not reuse api_id/api_hash you found on the internet.** Shared keys get
> flagged and banned, and the ban lands on the accounts using them.

## Why not the Bot API?

Because almost nothing this project is for is possible with a bot:

| | User account (MTProto) | Bot API |
|---|---|---|
| List your chats | yes | no |
| Read your history | yes | only messages sent to the bot |
| Search your messages | yes | no |
| See who you talk to | yes | no |
| Act as you | yes | no — acts as the bot |
| Read a group without being addressed | yes | only with privacy mode off, and only going forward |

A bot is a separate identity that gets told things. This project is about an
agent that can look through *your* account. That requires MTProto user
authentication. See [ADR 0001](decisions/0001-telegram-library.md) for the
library choice.

## Account safety

Telegram's anti-spam systems act on **accounts**, not applications. An automation
that misbehaves gets the *person* limited or banned, so a few precautions matter:

- **Start read-only.** Set `TGAGENT_PERMISSIONS__READ_ONLY_MODE=true` for the
  first few sessions and watch what the agent proposes to do.
- **Keep the outbound cap low.** `max_outbound_per_run` defaults to 20; lower it
  while you are building confidence.
- **Leave the write throttle on.** `min_seconds_between_writes` (default 1s)
  spaces out externally-visible operations specifically so a looping agent cannot
  trip flood detection.
- **Do not message strangers.** Unsolicited messages to people who have not
  contacted you is the single fastest route to a limitation.
- **Watch for `FLOOD_WAIT`.** tgagent surfaces these as a retryable error naming
  the wait time. If you see them often, you are asking for too much too quickly.

New accounts, and accounts that have recently changed number, are treated more
suspiciously by Telegram. Automating an account that is a few days old is asking
for trouble.

## Sessions

`tgagent login` writes `<data_dir>/sessions/tgagent.session`, a SQLite file
containing an **authorised session key**.

- It is equivalent to being logged in. Phone, password, and 2FA are not needed to
  use it.
- It is `.gitignore`d, and the directory is created `0700` on POSIX.
- It appears in Telegram under **Settings → Devices** as whatever
  `device_model` says (default: `tgagent`). Being honest here is deliberate —
  you want to recognise it when auditing your own account.
- Revoke it with `tgagent logout`, which terminates it **server-side** and then
  deletes the local file. Deleting the file alone leaves the session live.

You can also revoke it from any Telegram client: Settings → Devices → tap the
session → Terminate.

## Rate limits

Telegram enforces per-account and per-method limits that are undocumented and
change. Practically:

- Reading is generously limited; you will rarely hit it.
- Sending is limited aggressively, especially to people you have not talked to.
- Resolving usernames is limited — this is why tgagent caches resolutions for the
  process lifetime.
- Exceeding a limit returns `FLOOD_WAIT_X`, meaning "wait X seconds". Telethon
  sleeps through waits below `flood_sleep_threshold` (default 60s) transparently;
  longer ones surface to the agent, which reports them rather than hammering.

## Multiple accounts

Run separate data directories:

```bash
TGAGENT_DATA_DIR=~/.tgagent/personal tgagent run "…"
TGAGENT_DATA_DIR=~/.tgagent/work     tgagent run "…"
```

Each gets its own session, database, policy, memory, and audit log. Do not point
two processes at one data directory — SQLite tolerates it, but two schedulers
racing on the same tasks is not something you want to debug.
