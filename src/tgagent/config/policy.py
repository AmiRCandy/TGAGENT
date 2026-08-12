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
from typing import Any

import yaml

from tgagent.config.settings import PermissionSettings
from tgagent.errors import PolicyError
from tgagent.risk import PolicyDecision, RiskTier

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


def resolve_permissions(settings_permissions: PermissionSettings) -> PermissionSettings:
    """Apply ``policy_file`` if one is configured; otherwise pass through."""
    path = settings_permissions.policy_file
    if path is None:
        return settings_permissions
    path = Path(path).expanduser()
    if not path.exists():
        raise PolicyError(f"Configured policy file does not exist: {path}")
    return apply_policy(settings_permissions, load_policy_file(path), source=str(path))


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
