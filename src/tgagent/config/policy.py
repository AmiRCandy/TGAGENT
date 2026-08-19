"""Loading permission policy from YAML.

Policy is deliberately a *file*, separate from the rest of the configuration:
it is the thing an operator is most likely to review, diff, and check into
version control, and it must be editable without touching environment variables.

Example::

    read_only_mode: false
    non_interactive_decision: deny

    defaults:
      read_only: allow
      reversible: allow
      externally_visible: confirm
      destructive: confirm
      account_security: deny

    method_overrides:
      messages.SendMessage: confirm
      messages.DeleteHistory: deny
      channels.LeaveChannel: deny

    chat_allowlist: ["@alex", "-1001234567890"]
    chat_denylist: ["@work_announcements"]

    max_outbound_per_run: 10
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from tgagent.config.settings import PermissionSettings
from tgagent.errors import PolicyError
from tgagent.observability.logging import get_logger
from tgagent.risk import PolicyDecision, RiskTier

log = get_logger(__name__)

_SCALAR_FIELDS = {
    "read_only_mode": bool,
    "confirmation_timeout": float,
    "max_outbound_per_run": int,
}


def load_policy_file(path: Path) -> dict[str, Any]:
    """Read and shallow-validate a policy YAML file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"Cannot read policy file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"Policy file {path} is not valid YAML: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PolicyError(f"Policy file {path} must contain a mapping at the top level.")
    return data


def apply_policy(
    base: PermissionSettings, data: dict[str, Any], *, source: str
) -> PermissionSettings:
    """Return a copy of *base* with the policy mapping merged in.

    Unknown keys are an error rather than a silent no-op: a typo in a security
    policy that quietly does nothing is exactly the failure mode worth avoiding.
    """
    known = {
        "defaults",
        "method_overrides",
        "chat_allowlist",
        "chat_denylist",
        "non_interactive_decision",
        *_SCALAR_FIELDS,
    }
    if unknown := set(data) - known:
        raise PolicyError(
            f"Unknown key(s) in policy {source}: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}."
        )

    merged = base.model_copy(deep=True)

    if "defaults" in data:
        defaults = data["defaults"]
        if not isinstance(defaults, dict):
            raise PolicyError(f"`defaults` in {source} must be a mapping of tier -> decision.")
        for tier_name, decision_name in defaults.items():
            merged.defaults[_parse_tier(tier_name, source)] = _parse_decision(decision_name, source)

    if "method_overrides" in data:
        overrides = data["method_overrides"]
        if not isinstance(overrides, dict):
            raise PolicyError(f"`method_overrides` in {source} must be a mapping.")
        for method, decision_name in overrides.items():
            if not isinstance(method, str) or not method:
                raise PolicyError(f"Method override keys in {source} must be non-empty strings.")
            merged.method_overrides[method] = _parse_decision(decision_name, source)

    for key in ("chat_allowlist", "chat_denylist"):
        if key in data:
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise PolicyError(f"`{key}` in {source} must be a list of strings.")
            setattr(merged, key, list(value))

    if "non_interactive_decision" in data:
        merged.non_interactive_decision = _parse_decision(data["non_interactive_decision"], source)

    for key, caster in _SCALAR_FIELDS.items():
        if key in data:
            try:
                setattr(merged, key, caster(data[key]))
            except (TypeError, ValueError) as exc:
                raise PolicyError(
                    f"`{key}` in {source} must be a {caster.__name__}: {exc}"
                ) from exc

    return PermissionSettings.model_validate(merged.model_dump())


#: Overrides written by ``agent policy …`` from a chat. A *separate* file from the
#: operator's own policy, for three reasons: their file keeps its comments and its
#: place in version control, everything granted from a phone is visible in one
#: place, and revoking all of it is `rm`.
CHAT_POLICY_NAME: Final = "policy.chat.yaml"

_CHAT_POLICY_HEADER: Final = """\
# Permission overrides written by `agent policy …` from a Telegram chat.
#
# Managed by tgagent — edits here are kept, but the header may be rewritten.
# This file is applied *after* your own policy file, so it wins; it can only
# contain method_overrides, and it cannot loosen a method your policy file denies
# by name. Delete it to revoke everything granted remotely.
"""


def chat_policy_path(data_dir: Path) -> Path:
    return Path(data_dir).expanduser() / CHAT_POLICY_NAME


def load_chat_overrides(data_dir: Path) -> dict[str, PolicyDecision]:
    """The chat-written overrides, or ``{}``.

    A malformed file is a warning rather than a refusal to start: the operator's
    own policy is the security baseline and is still in force, and a process that
    will not boot is a worse outcome than a lost convenience.
    """
    path = chat_policy_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = load_policy_file(path)
    except PolicyError as exc:
        log.warning("policy.chat_overrides_unreadable", path=str(path), error=str(exc))
        return {}

    raw = data.get("method_overrides") or {}
    if not isinstance(raw, dict):
        log.warning("policy.chat_overrides_malformed", path=str(path))
        return {}
    resolved: dict[str, PolicyDecision] = {}
    for method, decision in raw.items():
        try:
            resolved[str(method)] = _parse_decision(decision, str(path))
        except PolicyError as exc:
            log.warning("policy.chat_override_ignored", method=method, error=str(exc))
    return resolved


def save_chat_override(data_dir: Path, method: str, decision: PolicyDecision | None) -> Path:
    """Write one override, or remove it when *decision* is ``None``."""
    path = chat_policy_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_chat_overrides(data_dir)
    if decision is None:
        current.pop(method, None)
    else:
        current[method] = decision

    body = {"method_overrides": {name: value.value for name, value in sorted(current.items())}}
    try:
        path.write_text(
            _CHAT_POLICY_HEADER + yaml.safe_dump(body, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        raise PolicyError(f"Cannot write {path}: {exc}") from exc
    return path


def resolve_permissions(
    settings_permissions: PermissionSettings, *, data_dir: Path | None = None
) -> PermissionSettings:
    """Apply ``policy_file``, then any chat-written overrides.

    The chat layer is applied last so that a change made from a phone takes
    effect. It cannot loosen a method the operator's file denies *by name* —
    that check lives in :mod:`tgagent.interfaces.admin`, at the point of writing,
    where there is somebody to tell.
    """
    resolved = settings_permissions
    path = settings_permissions.policy_file
    if path is not None:
        path = Path(path).expanduser()
        if not path.exists():
            raise PolicyError(f"Configured policy file does not exist: {path}")
        resolved = apply_policy(resolved, load_policy_file(path), source=str(path))

    if data_dir is not None and (chat := load_chat_overrides(data_dir)):
        resolved = apply_policy(
            resolved,
            {"method_overrides": {m: d.value for m, d in chat.items()}},
            source=str(chat_policy_path(data_dir)),
        )
        log.info("policy.chat_overrides_applied", count=len(chat))
    return resolved


def _parse_tier(name: Any, source: str) -> RiskTier:
    try:
        return RiskTier(str(name).strip().lower())
    except ValueError as exc:
        valid = ", ".join(t.value for t in RiskTier)
        raise PolicyError(f"Unknown risk tier {name!r} in {source}. Valid tiers: {valid}.") from exc


def _parse_decision(name: Any, source: str) -> PolicyDecision:
    try:
        return PolicyDecision(str(name).strip().lower())
    except ValueError as exc:
        valid = ", ".join(d.value for d in PolicyDecision)
        raise PolicyError(
            f"Unknown decision {name!r} in {source}. Valid decisions: {valid}."
        ) from exc
