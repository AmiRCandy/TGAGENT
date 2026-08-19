"""Settings changed at runtime, from wherever the operator happens to be.

Configuration normally comes from the environment and a ``.env`` file, which is
the right mechanism for a machine you can log into. It is the wrong one for the
situation this project is actually used in: the operator is holding a phone, in
Telegram, and the deployment is on a VPS somewhere.

So a *small, explicitly named* set of settings can be changed from a chat command
and are written here, next to the database, to survive a restart:

* ``llm.provider``, ``llm.model``, ``llm.api_key``, ``llm.base_url``

The allowlist is the security boundary and is deliberately tiny. Nothing about
permissions, the sandbox, or the trust boundary is settable this way — those are
the controls that make the rest of the system safe, and a chat message must not be
able to move them. Adding a key here is a security decision, not a convenience.

Precedence: this file wins over the environment. It has to, or "I just changed it"
would silently do nothing on a host that has ``TGAGENT_LLM__MODEL`` exported; the
command that writes it says so, and removing an entry restores the environment's
value.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from tgagent.errors import ConfigError
from tgagent.observability.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from tgagent.config.settings import Settings

log = get_logger(__name__)

#: Lives beside the database rather than in the working directory: a service's
#: cwd is not something the operator chose, and this has to be found again.
LOCAL_OVERRIDES_NAME: Final = "settings.local.json"

#: The complete set of settings changeable at runtime, as ``section.field``.
#: Read the module docstring before extending it.
SETTABLE: Final[frozenset[str]] = frozenset(
    {"llm.provider", "llm.model", "llm.api_key", "llm.base_url"}
)

#: Values never echoed back, and registered with the log redactor when applied.
SECRET_KEYS: Final[frozenset[str]] = frozenset({"llm.api_key"})


def local_overrides_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / LOCAL_OVERRIDES_NAME


def load_local_overrides(data_dir: Path) -> dict[str, Any]:
    """Read the file, or ``{}``. A corrupt file is a warning, not a crash.

    Failing to start because this file is malformed would be the worst possible
    trade: it holds conveniences, and the environment already carries a working
    configuration.
    """
    path = local_overrides_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("config.local_overrides_unreadable", path=str(path), error=str(exc))
        return {}
    if not isinstance(data, dict):
        log.warning("config.local_overrides_not_a_mapping", path=str(path))
        return {}
    return {key: value for key, value in data.items() if key in SETTABLE}


def save_local_override(data_dir: Path, key: str, value: str | None) -> Path:
    """Set or clear one setting. ``None`` removes it, restoring the environment."""
    if key not in SETTABLE:
        raise ConfigError(
            f"{key} cannot be changed at runtime. Settable: {', '.join(sorted(SETTABLE))}."
        )
    path = local_overrides_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_local_overrides(data_dir)
    if value is None:
        current.pop(key, None)
    else:
        current[key] = value

    try:
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # This file can hold an API key, so it gets the same treatment as the
        # session: owner-only, best effort, because not every filesystem obeys.
        with contextlib.suppress(OSError, NotImplementedError):
            path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"Cannot write {path}: {exc}") from exc
    return path


def apply_local_overrides(settings: Settings) -> Settings:
    """Merge the file into *settings*, in place, and return it.

    Applied after the environment so the file wins; see the module docstring for
    why that direction. Secrets are handed to the log redactor here, which is the
    one place that knows a value is a credential rather than a string.
    """
    from pydantic import SecretStr

    from tgagent.observability.redaction import secret_registry

    for key, value in load_local_overrides(settings.data_dir).items():
        section_name, _, field = key.partition(".")
        section = getattr(settings, section_name, None)
        if section is None or not hasattr(section, field):
            continue  # a key from a newer version; ignore rather than fail
        text = str(value)
        if key in SECRET_KEYS:
            secret_registry.register(text)
            setattr(section, field, SecretStr(text))
        else:
            setattr(section, field, text)
        log.info("config.local_override_applied", key=key)
    return settings


def describe_local_overrides(data_dir: Path) -> dict[str, str]:
    """What is set, with secrets masked. For showing the operator."""
    described: dict[str, str] = {}
    for key, value in load_local_overrides(data_dir).items():
        text = str(value)
        described[key] = mask(text) if key in SECRET_KEYS else text
    return described


def mask(secret: str) -> str:
    """Enough to recognise a key, never enough to use it."""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} chars)"


__all__ = [
    "LOCAL_OVERRIDES_NAME",
    "SECRET_KEYS",
    "SETTABLE",
    "apply_local_overrides",
    "describe_local_overrides",
    "load_local_overrides",
    "local_overrides_path",
    "mask",
    "save_local_override",
]
