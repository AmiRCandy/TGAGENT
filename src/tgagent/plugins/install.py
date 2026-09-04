"""Installing a plugin from a git URL, and removing one.

Installing a plugin means fetching code that will run with this account's
credentials. That cannot be made safe by validation, so this module does the two
things that *are* worth doing: it refuses anything it cannot identify, and it
records exactly what was installed.

* Only ``https://`` git URLs, only from hosts on an allow-list. No local paths,
  no ``file://``, no ``git://``, no ssh — those are how a "URL" becomes a path
  traversal or a silent read of something already on the box.
* A shallow clone into a temporary directory first, so a repository with no
  manifest, a bad manifest, or a name that collides is rejected before anything
  lands in the plugins directory.
* The resolved commit is recorded. "Which version is running" then has an answer
  that survives the upstream branch moving, and an upgrade is a deliberate
  reinstall rather than something that happens while you sleep.
* Nothing is executed during install. No setup script, no pip. The plugin's code
  runs when it is loaded, which is a separate step the operator controls.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from tgagent.observability.logging import get_logger
from tgagent.plugins.manifest import PluginError, PluginManifest, read_manifest
from tgagent.plugins.registry import InstalledPlugin, PluginState

log = get_logger(__name__)

#: Long enough for a slow clone, short enough that a hung git does not hold a
#: chat command open forever.
_CLONE_TIMEOUT = 120.0

_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)$")


@dataclass(slots=True, frozen=True)
class Installed:
    manifest: PluginManifest
    record: InstalledPlugin
    replaced: bool


def normalise_url(url: str, *, trusted_hosts: list[str]) -> str:
    """Turn what somebody typed into a URL worth cloning, or refuse it.

    ``owner/repo`` is accepted as GitHub shorthand because that is what people
    paste; everything else has to be an explicit https URL on a trusted host.
    """
    url = url.strip()
    if not url:
        raise PluginError("Give me the plugin's git URL, e.g. https://github.com/owner/repo.")

    if match := _SHORTHAND.match(url):
        url = f"https://github.com/{match.group(1)}/{match.group(2)}"

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise PluginError(
            f"Only https URLs can be installed, not {parsed.scheme or 'a bare path'!r}. "
            f"A local path or an ssh remote is not something I will fetch code from."
        )
    host = (parsed.hostname or "").lower()
    if host not in {h.lower() for h in trusted_hosts}:
        raise PluginError(
            f"{host or 'that host'} is not on plugins.trusted_hosts "
            f"({', '.join(trusted_hosts)}). Add it there if you mean to trust it."
        )
    if not parsed.path.strip("/"):
        raise PluginError(f"{url} does not name a repository.")
    return url


async def install(
    url: str,
    *,
    state: PluginState,
    trusted_hosts: list[str],
    max_installed: int,
    ref: str = "",
) -> Installed:
    """Clone *url*, validate it, and move it into place."""
    url = normalise_url(url, trusted_hosts=trusted_hosts)
    existing = state.all()

    scratch = Path(tempfile.mkdtemp(prefix="tgagent-plugin-"))
    try:
        await _clone(url, scratch / "repo", ref=ref)
        source = scratch / "repo"
        manifest = read_manifest(source)

        replaced = manifest.name in existing
        if not replaced and len([p for p in existing.values() if not p.builtin]) >= max_installed:
            raise PluginError(
                f"{max_installed} plugins are already installed, which is the configured "
                f"limit. Remove one first."
            )
        if (installed := existing.get(manifest.name)) is not None and installed.builtin:
            raise PluginError(
                f"{manifest.name!r} is the name of a plugin that ships with tgagent. "
                f"Ask the author to rename theirs."
            )

        commit = await _resolve_commit(source)
        # Everything under .git is a working copy of somebody else's history and
        # is not needed to run the plugin.
        shutil.rmtree(source / ".git", ignore_errors=True)

        destination = state.directory_for(manifest.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        shutil.move(str(source), str(destination))

        record = state.put(
            InstalledPlugin(
                name=manifest.name,
                source=url,
                enabled=True,
                ref=commit,
                config=dict(existing[manifest.name].config) if replaced else {},
            )
        )
        log.warning(
            "plugins.installed",
            plugin=manifest.name,
            version=manifest.version,
            source=url,
            ref=commit[:12],
            tools=list(manifest.tools),
        )
        return Installed(manifest=manifest, record=record, replaced=replaced)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def remove(name: str, *, state: PluginState) -> bool:
    """Forget a plugin and delete its directory.

    A built-in cannot be removed — there is nothing on disk to delete and the
    code ships with the agent — so it is switched off instead, which is what the
    caller wanted anyway.
    """
    record = state.get(name)
    if record is not None and record.builtin:
        state.set_enabled(name, False)
        return False

    directory = state.directory_for(name)
    shutil.rmtree(directory, ignore_errors=True)
    forgotten = state.forget(name)
    if forgotten:
        log.warning("plugins.removed", plugin=name)
    return forgotten


async def _clone(url: str, into: Path, *, ref: str = "") -> None:
    if shutil.which("git") is None:
        raise PluginError(
            "git is not installed on this machine, and it is how plugins are fetched. "
            "Install it (`apt install git`) and try again."
        )

    command = ["git", "clone", "--depth", "1", "--quiet"]
    if ref:
        command += ["--branch", ref]
    command += ["--", url, str(into)]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # No credential prompts: a private repository should fail cleanly rather
        # than hang waiting for a password nobody can type.
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "PATH": _path()},
    )
    try:
        _out, err = await asyncio.wait_for(process.communicate(), timeout=_CLONE_TIMEOUT)
    except TimeoutError as exc:
        process.kill()
        raise PluginError(f"Cloning {url} took longer than {_CLONE_TIMEOUT:.0f}s.") from exc

    if process.returncode != 0:
        detail = (err or b"").decode(errors="replace").strip().splitlines()
        raise PluginError(f"Cloning {url} failed: {detail[-1] if detail else 'git gave no reason'}")


async def _resolve_commit(repo: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        "rev-parse",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": _path()},
    )
    out, _err = await process.communicate()
    return (out or b"").decode(errors="replace").strip()


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


__all__ = ["Installed", "install", "normalise_url", "remove"]
