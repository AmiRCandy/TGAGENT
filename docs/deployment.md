# Deployment

Running tgagent unattended — which means the security posture matters more than
the convenience.

## Before anything else

An unattended agent has nobody to answer confirmation prompts. Decide
deliberately what it may do:

```yaml
# policy.yaml
non_interactive_decision: deny      # keep this unless you have a specific reason
read_only_mode: false

method_overrides:
  messages.SendMessage: allow       # only if the task genuinely needs it
  messages.DeleteHistory: deny

chat_allowlist: ["me"]              # and only into Saved Messages
max_outbound_per_run: 3
```

`chat_allowlist` is the highest-value control for unattended work: even a fully
compromised run can only write where you named.

## The short path: `./hermes deploy`

For a personal deployment on the machine you are already sitting at, the
repository's `hermes` script does the whole thing:

```bash
./hermes setup       # venv, dependencies, .env
./hermes login       # interactive, once
./hermes deploy      # systemd --user service: `tgagent listen --scheduler`
./hermes logs        # follow it
./hermes undeploy    # remove the service; session and database untouched
```

It installs a **user** service rather than a system one, deliberately: the agent
holds a personal Telegram session, so it belongs to a user account and not to
root. `deploy` refuses to install a service whose configuration is incomplete,
because a listener that cannot sign in would only crash-loop against Telegram.

Two things that script cannot decide for you. It enables the control bridge
(`tgagent listen`), which means chat-initiated runs *do* have a human to ask, so
read [Telegram control](telegram-control.md) before leaving it running. And user
services stop when your last session ends unless lingering is on:

```bash
sudo loginctl enable-linger "$USER"
```

Everything below is for the cases the script does not cover: a server you do not
log into, a shared host, or a container.

## Docker Compose (recommended)

```bash
cp .env.example .env                       # fill it in

# Sign in first — this needs a code from Telegram, so it must be interactive.
docker compose run --rm tgagent login

docker compose up -d
docker compose logs -f
```

The provided [`docker-compose.yml`](../docker-compose.yml) already sets:

- `read_only: true` with a small tmpfs on `/tmp`
- `cap_drop: ALL`, `no-new-privileges`
- memory and CPU limits
- log rotation
- a healthcheck
- a named volume for `/data`

Use `TGAGENT_SANDBOX__BACKEND=subprocess` inside the container. The `docker`
backend would need a socket mount, which is a far larger hole than it closes —
the container is already the boundary.

## systemd

```ini
# /etc/systemd/system/tgagent.service
[Unit]
Description=tgagent scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tgagent
Group=tgagent
WorkingDirectory=/srv/tgagent
Environment=TGAGENT_DATA_DIR=/srv/tgagent/data
Environment=TGAGENT_LOGGING__FORMAT=json
Environment=TGAGENT_PERMISSIONS__POLICY_FILE=/srv/tgagent/policy.yaml
EnvironmentFile=/srv/tgagent/.env
# `serve` runs scheduled tasks only. Append the control bridge with
# TGAGENT_CONTROL__ENABLED=true, or run `tgagent listen --scheduler` instead —
# but note that gives anyone who can post in an allowed chat a way to start runs.
ExecStart=/srv/tgagent/venv/bin/tgagent serve
Restart=on-failure
RestartSec=30

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/tgagent/data
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryMax=1G
CPUQuota=150%

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd --system --home /srv/tgagent --shell /usr/sbin/nologin tgagent
sudo -u tgagent /srv/tgagent/venv/bin/tgagent login    # interactive, once
sudo chmod 600 /srv/tgagent/data/sessions/*.session
sudo systemctl enable --now tgagent
journalctl -u tgagent -f
```

## Getting the session onto a server

Sign-in needs a code that only reaches your phone, so there is no headless path.
Sign in on a workstation and copy the file:

```bash
tgagent login
scp ~/.local/share/tgagent/sessions/tgagent.session server:/srv/tgagent/data/sessions/
ssh server 'chmod 600 /srv/tgagent/data/sessions/tgagent.session && chown tgagent: $_'
```

Or, for Docker, `docker compose run --rm tgagent login` into the same volume the
daemon uses.

## Operating

### Logs

```bash
TGAGENT_LOGGING__FORMAT=json
```

Structured events with a `run_id` on every line, so a run is greppable
end-to-end. Notable events: `gateway.call`, `gateway.denied`,
`sandbox.suspicious_content`, `scheduler.task_started`, `agent.run_finished`.

Secrets are redacted in the pipeline, so this is safe to ship to a log
aggregator. Message *content* is not logged by default (`log_call_arguments` is
off) — leave it that way if logs leave the host.

### Health

`tgagent version` is the healthcheck: it exits non-zero if the install is broken.
For liveness, watch for `scheduler.task_started` at the cadence you expect.

### The audit log is the record

```bash
tgagent audit -n 100
tgagent audit --run <run-id>
```

Review it periodically. It is where an injection attempt or an unexpected
approval shows up.

## Backups

```bash
#!/bin/sh
set -eu
D=/srv/tgagent/data
B=/backups/tgagent
mkdir -p "$B"
sqlite3 "$D/tgagent.db" ".backup $B/tgagent-$(date +%F).db"   # transactional, safe while running
cp "$D"/sessions/*.session "$B/"
chmod 600 "$B"/*
find "$B" -name 'tgagent-*.db' -mtime +30 -delete
```

> The session file **is a credential**. An unencrypted backup of it is an
> unencrypted copy of your Telegram account. Encrypt the backup destination.

## Upgrading

```bash
systemctl stop tgagent
sqlite3 data/tgagent.db ".backup /backups/pre-upgrade.db"
venv/bin/pip install --upgrade "tgagent[anthropic]"
systemctl start tgagent
```

Migrations run automatically. Forward-only, so downgrading after an upgrade
refuses to start rather than corrupting data — keep the backup.

## Hardening checklist

- [ ] Policy file in place, reviewed, `non_interactive_decision: deny`
- [ ] `chat_allowlist` set if the agent may write at all
- [ ] `max_outbound_per_run` at the smallest workable value
- [ ] Dedicated unprivileged user; data directory `0700`, session `0600`
- [ ] Full-disk encryption, or at least an encrypted data volume
- [ ] `sandbox.backend: docker` where the host is shared or untrusted
- [ ] `logging.format: json`, `log_call_arguments: false`
- [ ] Encrypted, tested backups
- [ ] Audit review on a schedule you will actually keep
- [ ] Alerting on `gateway.denied` and `sandbox.suspicious_content`

## Scaling

There is nothing to scale horizontally: one Telegram account, one agent. If you
need several accounts, run several instances, each with its own
`TGAGENT_DATA_DIR`. Do not point two processes at one data directory — claiming
makes it *safe*, but debugging two schedulers racing is not how you want to spend
an afternoon.
