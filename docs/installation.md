# Installation

## Requirements

- **Python 3.11 or newer.** 3.11 is the floor because the project uses
  `asyncio.timeout`, `StrEnum`, and `datetime.UTC`.
- **A Telegram account** — a real one, with a phone number. See
  [Telegram setup](telegram-setup.md).
- **An LLM provider key** — see [LLM providers](llm-providers.md).
- **Docker** (optional) for the strongest [sandbox](sandboxing.md).

## Install

### From a clone: one command

If you have the repository, the installer asks for what it needs and gets you to a
signed-in agent:

```bash
git clone https://github.com/tgagent/tgagent && cd tgagent
./install.sh
```

It creates `./.venv`, installs the project with development extras, prompts for
your Telegram `api_id`/`api_hash` and a model provider — writing them into `.env`
with mode `600` — verifies the result with `tgagent config check`, and offers to
sign in. Nothing is overwritten without asking, so running it again is safe: it
reports what is already configured and offers to keep it.

Secrets are read without echoing, and written by a Python helper rather than
`sed`, so an API key containing `&`, `/`, or `"` lands intact instead of becoming
a confusing authentication error later.

Without a terminal (CI, a provisioning script) it installs the environment, seeds
`.env`, and stops rather than blocking on a prompt.

`./install.sh` is a two-line alias for `./hermes install`; `./hermes` is the same
script's other commands — `check`, `listen`, `deploy`, `logs`. See
[Deployment](deployment.md).

### As a package

```bash
# Anthropic (the default provider)
pip install "tgagent[anthropic]"

# OpenAI, or any OpenAI-compatible endpoint
pip install "tgagent[openai]"

# Everything, including the Rust crypto accelerator and proxy support
pip install "tgagent[all]"
```

### Extras

| Extra | Pulls in | Why |
|---|---|---|
| `anthropic` | `anthropic` | The default provider adapter |
| `openai` | `openai` | OpenAI and every OpenAI-compatible gateway |
| `speedups` | `cryptg` | Rust AES-IGE. Large speedup on media transfer; needs a compiler where no wheel exists |
| `proxy` | `PySocks` | SOCKS/HTTP proxy for the MTProto connection |
| `all` | all of the above | |

The base install has no optional dependencies, so `pip install tgagent` gives you
a working CLI that can do everything except talk to a model — useful for
inspecting configuration and policy.

### With pipx (recommended for a CLI)

```bash
pipx install "tgagent[anthropic]"
```

Keeps tgagent and its dependencies out of your system or project environments.

### From source

```bash
git clone https://github.com/tgagent/tgagent
cd tgagent
pip install -e ".[dev]"
```

See [development](development.md) for the full contributor setup.

## Verify

```console
$ tgagent version
tgagent 0.1.0
python  3.12.3
telethon 1.44.0

$ tgagent config check
```

`config check` prints a table of what is configured and what is missing, and
exits non-zero if Telegram credentials are absent. That is the fastest way to
find out what is left to do.

```console
$ tgagent sandbox
```

Reports what the configured sandbox backend actually isolates, then runs live
probes proving that filesystem, `os`, and network access are refused. Worth
running once after install — it is the only way to *see* the isolation rather
than take it on trust.

## First run

```bash
cp .env.example .env       # fill in api_id, api_hash, and your LLM key
tgagent login              # phone → code → 2FA password if you have one
tgagent run "list my 5 most recent chats"
```

## Docker

```bash
cp .env.example .env       # fill it in

# Sign in first — this needs a code from Telegram, so it must be interactive.
docker compose run --rm tgagent login

# Then run the scheduler daemon.
docker compose up -d
docker compose logs -f
```

The session and database live in the `tgagent-data` volume. Back it up like a
key store, because that is what it is. See [deployment](deployment.md).

## Upgrading

```bash
pip install --upgrade "tgagent[anthropic]"
```

The database migrates itself on the next start. Migrations are forward-only, so
a downgrade after an upgrade will refuse to start rather than corrupt data:

```
Database schema version 2 is newer than this build supports (1).
```

Your session file is unaffected by upgrades — you do not need to sign in again.

## Uninstalling

```bash
tgagent logout             # revokes the session on Telegram's side, not just locally
pip uninstall tgagent
rm -rf ~/.local/share/tgagent      # Linux
rm -rf ~/Library/Application\ Support/tgagent   # macOS
# Windows: %APPDATA%\tgagent
```

Run `logout` **before** deleting the data directory. Deleting the session file
alone leaves an authorised session listed on your account forever.
