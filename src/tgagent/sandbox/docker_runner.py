"""Docker sandbox backend — real isolation, when you need it.

Same protocol as the subprocess backend, same worker script, but the child runs
inside a container with hard boundaries the host OS enforces rather than ones
CPython politely observes:

* ``--network none`` — no network stack at all. Not "no sockets we know about":
  no interfaces, no DNS, no route. This is the guarantee the subprocess backend
  cannot make.
* ``--read-only`` with a small ``tmpfs`` on ``/tmp`` — the filesystem is not
  writable, so a program cannot persist anything.
* ``--memory`` / ``--cpus`` / ``--pids-limit`` — enforced by cgroups, on every
  platform including Windows and macOS via Docker Desktop.
* ``--cap-drop ALL`` and ``--security-opt no-new-privileges``.
* ``--user 65534:65534`` (nobody) — no root inside the container either.

The trade-off is startup latency (a few hundred milliseconds per execution) and
the requirement that Docker be installed. That is why ``subprocess`` remains the
default and this is the recommended backend for anything unattended.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from tgagent.config.settings import SandboxSettings
from tgagent.errors import SandboxUnavailable
from tgagent.observability.logging import get_logger
from tgagent.sandbox.subprocess_runner import WORKER_PATH, SubprocessSandbox, stream_limit

log = get_logger(__name__)


class DockerSandbox(SubprocessSandbox):
    """Runs the worker inside a locked-down container."""

    name = "docker"

    def __init__(self, settings: SandboxSettings) -> None:
        super().__init__(settings)
        self._docker = shutil.which("docker")
        if self._docker is None:
            raise SandboxUnavailable(
                "The docker sandbox backend was selected but the `docker` executable "
                "was not found on PATH. Install Docker, or set "
                "TGAGENT_SANDBOX__BACKEND=subprocess."
            )

    def describe_isolation(self) -> str:
        s = self._settings
        return (
            f"Container from {s.docker_image} with --network={s.docker_network} (no network "
            f"stack), a read-only root filesystem, {s.max_memory_mb}MB memory and "
            f"{s.max_cpu_seconds}s CPU caps enforced by cgroups, all capabilities dropped, "
            f"no-new-privileges, and a non-root user. The worker inside still holds no "
            f"credentials and reaches Telegram only through the policed RPC pipe."
        )

    async def _spawn(self, workdir: Path) -> asyncio.subprocess.Process:
        assert self._docker is not None
        s = self._settings

        argv = [
            self._docker, "run",
            "--rm",
            "--interactive",
            f"--network={s.docker_network}",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            f"--memory={s.max_memory_mb}m",
            f"--memory-swap={s.max_memory_mb}m",
            "--pids-limit=64",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            # The worker is mounted read-only; nothing else from the host is visible.
            "--volume", f"{WORKER_PATH}:/opt/worker.py:ro",
            "--workdir", "/tmp",
            "--env", "PYTHONHASHSEED=0",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1",
            *s.docker_extra_args,
            s.docker_image,
            "python", "-I", "/opt/worker.py",
        ]

        log.debug("sandbox.docker_spawn", image=s.docker_image, network=s.docker_network)
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=stream_limit(s),
        )
