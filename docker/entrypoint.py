#!/usr/bin/env python
"""Container entrypoint: prepare the data directory, then drop privileges.

Exists because of two facts about managed platforms that a plain ``USER`` line in
a Dockerfile cannot satisfy:

1. **A platform-attached volume arrives owned by root.** Railway, Fly and friends
   mount the volume after the image is built, so the ``chown`` baked into the
   image applies to an empty directory that the mount then hides. A container
   that starts as a non-root user simply cannot write to it. The fix is to start
   as root, take ownership of the mount, and *then* become the unprivileged user
   — which is what this script does, in that order.

2. **There is no terminal to log in from.** Telegram sign-in is interactive: it
   needs a code from another device, and there is nowhere to type one. So the
   session is created once on a machine that does have a terminal and delivered
   here as ``TGAGENT_SESSION_B64``. This script materialises it into the volume
   on first boot, after which the volume is the source of truth and the variable
   is ignored.

Written in Python rather than shell because the image already has a Python and
does not have ``gosu`` — and because ``os.setuid`` is a more honest way to drop
privileges than hoping the right helper binary is installed.

It is a no-op when already running unprivileged, so it is safe to use as the
entrypoint everywhere, not only on a platform that needs it.
"""

from __future__ import annotations

import base64
import binascii
import grp
import os
import pwd
import stat
import sys
from pathlib import Path

RUN_AS = os.environ.get("TGAGENT_RUN_AS", "tgagent")
DATA_DIR = Path(os.environ.get("TGAGENT_DATA_DIR", "/data"))
SESSION_B64_VAR = "TGAGENT_SESSION_B64"

#: Every Telethon session is a SQLite database. Checking the magic catches the
#: single most likely mistake — a truncated or re-wrapped copy-paste — at boot,
#: with a clear message, instead of as a confusing Telethon error later.
SQLITE_MAGIC = b"SQLite format 3\x00"


def log(message: str) -> None:
    # stderr, unbuffered: this runs before the application's logging exists, and
    # a platform's log viewer should show it interleaved correctly regardless.
    print(f"entrypoint: {message}", file=sys.stderr, flush=True)


def die(message: str) -> None:
    log(f"error: {message}")
    raise SystemExit(1)


def resolve_account() -> tuple[int, int] | None:
    """The uid/gid to run as, or ``None`` to stay as we are."""
    if RUN_AS in ("", "root", "0"):
        return None
    try:
        entry = pwd.getpwnam(RUN_AS)
    except KeyError:
        die(f"no such user {RUN_AS!r} in the image")
    return entry.pw_uid, entry.pw_gid


def take_ownership(path: Path, uid: int, gid: int) -> None:
    """Make *path* usable by the unprivileged user.

    The recursive walk runs only when the directory itself was owned by someone
    else, which is the first-boot case on a fresh platform volume. On every later
    boot the top-level owner is already correct and the walk is skipped — worth
    caring about, because the tree can hold a lot of downloaded media and a
    recursive chown of it on every restart is pure latency.
    """
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    already_ours = info.st_uid == uid and info.st_gid == gid

    os.chown(path, uid, gid)
    # 0700: the directory holds the session file, which is a live credential.
    os.chmod(path, stat.S_IRWXU)

    if already_ours:
        return

    log(f"taking ownership of {path} (first boot on this volume)")
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            target = Path(root) / name
            try:
                os.chown(target, uid, gid, follow_symlinks=False)
            except OSError as exc:  # pragma: no cover - platform-specific
                log(f"warning: could not chown {target}: {exc}")


def install_session(session_dir: Path) -> None:
    """Write the session from the environment, if one was supplied and none exists."""
    encoded = os.environ.get(SESSION_B64_VAR, "").strip()
    if not encoded:
        return

    session_dir.mkdir(parents=True, exist_ok=True)
    name = os.environ.get("TGAGENT_TELEGRAM__SESSION_NAME", "tgagent")
    destination = session_dir / f"{name}.session"

    if destination.exists():
        # The volume wins. Overwriting a working session with a stale variable
        # would log the account out of the session it is actually using.
        log(f"{destination.name} already present; ignoring {SESSION_B64_VAR}")
        return

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        die(
            f"{SESSION_B64_VAR} is not valid base64 ({exc}). Regenerate it with "
            f"`./hermes session-export` and paste the whole single line."
        )

    if not raw.startswith(SQLITE_MAGIC):
        die(
            f"{SESSION_B64_VAR} did not decode to a Telethon session (a SQLite "
            f"database). It was probably truncated in transit — regenerate it with "
            f"`./hermes session-export` and check the value is complete."
        )

    # Written 0600 from the start: never briefly world-readable.
    handle = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(handle, raw)
    finally:
        os.close(handle)
    log(f"installed session from {SESSION_B64_VAR} ({len(raw)} bytes)")


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])  # no inherited supplementary groups
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:  # pragma: no cover - defensive
        die("failed to drop privileges")
    log(f"running as {RUN_AS} ({uid}:{gid})")


def main(argv: list[str]) -> int:
    command = argv or ["serve"]
    account = resolve_account()

    if os.geteuid() == 0 and account is not None:
        uid, gid = account
        take_ownership(DATA_DIR, uid, gid)
        install_session(DATA_DIR / "sessions")
        # chown once more: install_session may have created the directory as root.
        take_ownership(DATA_DIR, uid, gid)
        drop_privileges(uid, gid)
    else:
        # Unprivileged already — an ordinary `docker run --user`, or a platform
        # that starts containers as non-root. Nothing to fix, and the session can
        # still be installed if the directory happens to be writable.
        if os.access(DATA_DIR.parent, os.W_OK) or DATA_DIR.exists():
            install_session(DATA_DIR / "sessions")

    os.execvp(command[0], command)  # noqa: S606 - the image's own entrypoint


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
