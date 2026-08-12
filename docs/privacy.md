# Privacy considerations

This agent reads a real person's private messages. Some of that data leaves your
machine. This page is about exactly what, and what you can do about it.

## What leaves your machine

### To your LLM provider

**Whatever the agent reads.** If it searches your chat with Alex, the matching
message text goes into a prompt and is sent to the provider.

This is inherent to using a hosted model, and it is the single most important
privacy fact about this project.

What that means concretely:

- Message text, chat names, usernames, and filenames the agent retrieves.
- Your own instructions.
- Your account's id and username (in the system prompt, so the agent can tell
  your messages from other people's).

What is **not** sent:

- Your `api_hash`, session key, or 2FA password.
- File *contents*. Media is described by metadata (name, MIME, size) and
  downloaded to disk; the bytes never enter a prompt.
- Anything the agent did not retrieve. Nothing is uploaded proactively.

**Third parties did not consent.** The people who messaged you did not agree to
their words being sent to an AI provider. Whether that matters legally depends on
where you and they are; whether it matters ethically is not really in doubt.

Provider retention policies vary and change. Check your provider's terms.

### To Telegram

The operations the agent performs — which are operations you could perform
yourself. Telegram sees the same traffic your official client would, plus the
device string you configured (default `tgagent`).

### To nowhere else

There is no telemetry, no analytics, no crash reporting, and no phone-home. The
project makes exactly two kinds of outbound connection: Telegram, and your
configured LLM provider. The sandbox makes none at all.

## What is stored locally

`<data_dir>/`:

| Path | Contains |
|---|---|
| `sessions/*.session` | **An authenticated session key.** Equivalent to being logged in. |
| `tgagent.db` | Conversations (including message excerpts the agent quoted), memory facts, tasks, audit log |
| `media/<run-id>/` | Downloaded files, verbatim |
| `cache/telethon-schema.json` | The API index. No personal data |

The database is **not encrypted**. Use full-disk encryption; the session file
alone is enough to take over the account.

## Minimising exposure

### Run the model locally

The complete answer to "my messages go to a provider":

```bash
TGAGENT_LLM__PROVIDER=ollama
TGAGENT_LLM__BASE_URL=http://localhost:11434/v1
TGAGENT_LLM__MODEL=llama3.3:70b
TGAGENT_LLM__CONTEXT_WINDOW=128000
```

Nothing leaves your machine except Telegram traffic. The trade-off is real —
smaller models are noticeably worse at tool calling, and often at generated code
specifically. Consider `TGAGENT_FEATURES__CODE_EXECUTION=false` with a local
model and let it use the curated tools.

### Scope requests narrowly

The agent sends what it reads. "Search my chat with @alex for the migration"
exposes far less than "search everything for anything interesting" — and gets a
better answer.

### Turn off argument logging

It is off by default (`logging.log_call_arguments: false`), which is why the
audit log stores an argument *digest* rather than message text. Leave it off
unless you specifically need it.

### Shorten retention

```bash
TGAGENT_MEDIA__RETENTION_DAYS=1        # reap downloads daily
TGAGENT_STORAGE__AUDIT_RETENTION_DAYS=7
```

Clear conversation history when you no longer need it:

```bash
sqlite3 tgagent.db "DELETE FROM conversations;"   # cascades to messages
```

### Restrict what it can reach

`chat_allowlist` limits *writes*, not reads. To limit what is read, be specific
in your requests — or run read-only and review each summary.

## Other people's data

Your Telegram history is full of other people's words, phone numbers, and files.

- **Group chats** mean everyone in them. A request to "summarise the team
  channel" sends other people's messages to your provider.
- **Contacts** — names and usernames are read; phone numbers are masked to the
  last four digits wherever they are serialised.
- **Media** other people sent lands on your disk and stays until reaped.

Under GDPR and similar regimes, processing this data for personal purposes is
generally covered by the household exemption. Using it for business purposes
probably is not, and using it to build a profile of someone certainly is not.
This is not legal advice; if you are operating in a professional context, get
some.

## What tgagent does to help

- **Phone masking** — `…4567`, never the full number.
- **Secret redaction** — every log line, including from third-party libraries,
  passes through exact-value and pattern-based redaction.
- **No arguments in the audit log by default** — a digest proves two calls were
  identical without storing the text.
- **Media retention** — downloads are reaped on a schedule.
- **Truncation** — long text is capped before it enters a prompt.
- **No telemetry** — nothing is collected about you at all.

## Deleting everything

```bash
tgagent logout                        # revoke server-side first
rm -rf ~/.local/share/tgagent         # session, database, media, cache
```

Then, separately, ask your LLM provider to delete data under their retention
policy — tgagent cannot do that for you.

## A note on consent

The intended use is automating **your own** account. Using it to operate someone
else's account without their knowledge, to monitor a partner or an employee, or
to harvest a group's messages is a misuse of it, a Telegram terms-of-service
violation, and in many jurisdictions illegal. The permission system exists to
protect the account owner — it is not a mechanism for making surveillance
convenient.
