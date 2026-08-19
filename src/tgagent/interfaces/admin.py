"""Changing policy and model settings from wherever the operator is.

The terminal is where this software is configured and the phone is where it is
used, and until now those were the same place only by accident. This module is the
narrow bridge: two families of chat command, ``agent policy …`` and ``agent llm …``,
that change what the running process is allowed to do and which model it talks to.

Both are answered by the bridge itself, never by a tool. That distinction is the
whole security argument:

* A tool call can be *reached* by content — a model reading a message decides to
  call it, and prompt injection has a path. A built-in word is parsed from a
  message that already passed the authorship check, so only the account owner's
  own messages can reach this code at all.
* On top of that, both families are **owner-only**. ``control.allowed_senders``
  grants somebody the ability to spend your tokens and act as your account; it
  does not extend to rewriting your permission policy or pointing the model at a
  different endpoint, and the two failures are not remotely comparable in size.

What can be changed, and what cannot
------------------------------------
Loosening is bounded; tightening never is. From a chat you may:

* set any method to ``allow``, ``confirm``, or ``deny`` — except the operations
  that can lock you out of the account (:func:`~tgagent.security.permissions.grantable`),
  and except any method your own policy file denies *by name*;
* change the model, provider, API key, and base URL — the four settings in
  :data:`~tgagent.config.local.SETTABLE`.

You may not touch anything else: not the sandbox, not the trust boundary, not
``read_only_mode``, not the chat allow/denylists. Those are the controls that make
the rest safe, and a message must not be able to move them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from tgagent.config.local import (
    SECRET_KEYS,
    describe_local_overrides,
    load_local_overrides,
    mask,
    save_local_override,
)
from tgagent.config.policy import chat_policy_path, load_chat_overrides, save_chat_override
from tgagent.config.settings import Settings
from tgagent.observability.logging import get_logger
from tgagent.risk import PolicyDecision, RiskTier
from tgagent.security.permissions import (
    PermissionEngine,
    PolicyExplanation,
    classify,
    grantable,
)

log = get_logger(__name__)

#: What ``agent llm <name> …`` accepts, mapped to the settings key it writes.
_LLM_FIELDS: dict[str, str] = {
    "model": "llm.model",
    "provider": "llm.provider",
    "key": "llm.api_key",
    "api_key": "llm.api_key",
    "apikey": "llm.api_key",
    "url": "llm.base_url",
    "api_url": "llm.base_url",
    "base_url": "llm.base_url",
    "endpoint": "llm.base_url",
}

_DECISIONS: dict[str, PolicyDecision] = {
    "allow": PolicyDecision.ALLOW,
    "confirm": PolicyDecision.CONFIRM,
    "ask": PolicyDecision.CONFIRM,
    "deny": PolicyDecision.DENY,
    "block": PolicyDecision.DENY,
}


@dataclass(slots=True, frozen=True)
class AdminResult:
    """What to say back, and whether anything actually changed."""

    message: str
    changed: bool = False
    #: True when the command carried a credential, so the message that contained
    #: it should not be left sitting in the chat history.
    contained_secret: bool = False


class RuntimeAdmin:
    """Reads and changes policy and model settings for a running process.

    Holds the live :class:`~tgagent.security.permissions.PermissionEngine` and
    settings rather than the application, so the interface layer stays unaware of
    the composition root — and so this is testable without one.
    """

    def __init__(
        self,
        settings: Settings,
        permissions: PermissionEngine,
        *,
        on_llm_changed: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._permissions = permissions
        #: Called after a model setting changes, so the next run builds a fresh
        #: provider. Without it the process would report a new model and keep
        #: using the old one, which is worse than refusing the change.
        self._on_llm_changed = on_llm_changed

    # --------------------------------------------------------------- policy ---
    def policy(self, argument: str) -> AdminResult:
        """Handle ``policy``, ``policy <method>``, ``policy <decision> <method>``."""
        words = argument.split()
        if not words:
            return AdminResult(self._policy_overview())

        head = words[0].casefold()
        # `policy add send_message` reads naturally and means allow; so does
        # `policy allow send_message`. Both are accepted, because the operator
        # should not have to remember which verb this software prefers.
        if head in ("add", "grant"):
            head, words = "allow", ["allow", *words[1:]]
        if head in ("remove", "reset", "clear", "unset", "revoke"):
            if len(words) < 2:
                return AdminResult(self._policy_usage("Say which method to reset."))
            return self._clear(words[1])
        if head in _DECISIONS:
            if len(words) < 2:
                return AdminResult(self._policy_usage(f"Say what to {head}."))
            return self._set(words[1], _DECISIONS[head])
        if len(words) == 1:
            return AdminResult(self._explain(words[0]))
        return AdminResult(self._policy_usage(f"I do not know what {head!r} means here."))

    def _set(self, method: str, decision: PolicyDecision) -> AdminResult:
        method = method.strip()
        explanation = self._permissions.explain(method)

        # Tightening is always allowed — it can only ever reduce what the account
        # can do, so there is nothing to protect against.
        if decision is not PolicyDecision.DENY:
            if (refusal := grantable(method, self._operators_view(explanation))) is not None:
                return AdminResult(f"❌ {refusal}")
            if self._is_unknown(method):
                return AdminResult(
                    f"❌ `{method}` is not a Telegram method I recognise, so allowing it "
                    f"would be allowing nothing — check the spelling with "
                    f"`tgagent config policy {method}`, or ask me to search the API for it."
                )

        path = save_chat_override(self._settings.data_dir, method, decision)
        # Applied to the live engine as well as the file: a policy change that
        # needed a restart would be useless from a phone, which is the only place
        # this command exists for.
        self._permissions.settings.method_overrides[method] = decision
        log.warning(
            "admin.policy_changed",
            method=method,
            decision=decision.value,
            risk=explanation.risk.value,
            path=str(path),
        )
        verdict = self._permissions.explain(method)
        return AdminResult(
            f"✅ `{method}` → **{decision.value}** (risk: {explanation.risk.value})\n"
            f"In force now, and after a restart.\n"
            f"{self._effect_line(verdict.decision)}\n"
            f"_Written to {path.name}; `{self._trigger()} policy remove {method}` undoes it._",
            changed=True,
        )

    def _operators_view(self, explanation: PolicyExplanation) -> PolicyExplanation:
        """The same explanation with chat-written overrides discounted.

        "A grant cannot lift what you forbade" has to mean the rule *you* wrote in
        your own policy file, not one this command wrote a minute ago. Without
        this, ``policy deny X`` from a chat would be a one-way door: the deny it
        just created would be the reason it refused to undo it, and the way out
        would be a terminal.
        """
        chat_set = load_chat_overrides(self._settings.data_dir)
        return replace(
            explanation,
            matched_overrides=tuple(
                name for name in explanation.matched_overrides if name not in chat_set
            ),
        )

    def _clear(self, method: str) -> AdminResult:
        method = method.strip()
        if method not in load_chat_overrides(self._settings.data_dir):
            return AdminResult(
                f"`{method}` was not set from a chat, so there is nothing to reset here. "
                f"{self._explain(method)}"
            )
        save_chat_override(self._settings.data_dir, method, None)
        self._permissions.settings.method_overrides.pop(method, None)
        log.warning("admin.policy_reset", method=method)
        return AdminResult(
            f"✅ `{method}` is back to its default: "
            f"**{self._permissions.explain(method).decision.value}**.",
            changed=True,
        )

    def _explain(self, method: str) -> str:
        explanation = self._permissions.explain(method)
        lines = [
            f"`{method}`",
            f"· risk: **{explanation.risk.value}**",
            f"· decision: **{explanation.decision.value}**",
        ]
        if explanation.matched_overrides:
            lines.append(f"· set by: {', '.join(explanation.matched_overrides)}")
        lines.append(self._effect_line(explanation.decision))
        return "\n".join(lines)

    def _policy_overview(self) -> str:
        settings = self._permissions.settings
        lines = ["**Permission policy**"]
        for tier in RiskTier:
            decision = settings.defaults.get(tier, PolicyDecision.DENY)
            lines.append(f"· {tier.value}: **{decision.value}**")
        if settings.read_only_mode:
            lines.append("\n⚠️ **read-only mode** — every write is refused.")
        lines.append(f"\nUnattended runs: **{settings.non_interactive_decision.value}**")

        chat_set = load_chat_overrides(self._settings.data_dir)
        if settings.method_overrides:
            lines.append("\n**Per-method**")
            for method, decision in sorted(settings.method_overrides.items()):
                mark = " _(from chat)_" if method in chat_set else ""
                lines.append(f"· `{method}`: **{decision.value}**{mark}")
        trigger = self._trigger()
        lines.append(
            f"\n`{trigger} policy allow <method>` · `{trigger} policy deny <method>` · "
            f"`{trigger} policy <method>` to check one"
        )
        return "\n".join(lines)

    def _policy_usage(self, problem: str) -> str:
        trigger = self._trigger()
        return (
            f"{problem}\n\n"
            f"`{trigger} policy` — the whole policy\n"
            f"`{trigger} policy <method>` — what would happen\n"
            f"`{trigger} policy allow <method>` — permit it (also `add`)\n"
            f"`{trigger} policy confirm <method>` — ask me each time\n"
            f"`{trigger} policy deny <method>` — refuse it\n"
            f"`{trigger} policy remove <method>` — back to the default\n\n"
            f"Methods are named either way: `send_message` or `messages.SendMessage`."
        )

    @staticmethod
    def _effect_line(decision: PolicyDecision) -> str:
        return {
            PolicyDecision.ALLOW: "Runs with nobody attached can do this now.",
            PolicyDecision.CONFIRM: "I will ask you each time; unattended runs are refused.",
            PolicyDecision.DENY: "Refused everywhere, including when you are here.",
        }[decision]

    def _is_unknown(self, method: str) -> bool:
        """Whether this looks like a method name nothing will ever match.

        An `allow` on a typo is the quiet failure this catches: the policy grows an
        entry, the operator believes the job is permitted, and the call it actually
        makes is still refused. Classification's own fail-safe is what gives it
        away — an unrecognised, write-shaped name lands in `destructive` with no
        table entry behind it.
        """
        from tgagent.security.permissions import (
            _ACCOUNT_SECURITY_METHODS,
            _DESTRUCTIVE_METHODS,
            _EXTERNALLY_VISIBLE_METHODS,
            _READ_ONLY_METHODS,
            _REVERSIBLE_METHODS,
            canonical_method_key,
            normalise_method,
        )

        _, key = canonical_method_key(method)
        _, bare = normalise_method(method)
        known = (
            _ACCOUNT_SECURITY_METHODS
            | _DESTRUCTIVE_METHODS
            | _EXTERNALLY_VISIBLE_METHODS
            | _REVERSIBLE_METHODS
            | _READ_ONLY_METHODS
        )
        return key not in known and bare not in known and classify(method) is RiskTier.DESTRUCTIVE

    # ------------------------------------------------------------------ llm ---
    def llm(self, argument: str) -> AdminResult:
        """Handle ``llm``, ``llm <field> <value>``, ``llm reset <field>``."""
        words = argument.split(maxsplit=1)
        if not words:
            return AdminResult(self._llm_overview())

        head = words[0].casefold().replace("-", "_")
        rest = words[1].strip() if len(words) > 1 else ""

        if head in ("reset", "clear", "unset", "remove"):
            field = _LLM_FIELDS.get(rest.split()[0].casefold()) if rest else None
            if field is None:
                return AdminResult(self._llm_usage("Say which setting to reset."))
            save_local_override(self._settings.data_dir, field, None)
            self._reload_llm()
            return AdminResult(
                f"✅ `{field}` is back to whatever the environment says. "
                f"Restart to be certain it took, since the environment is only read at "
                f"start-up.",
                changed=True,
            )

        key = _LLM_FIELDS.get(head)
        if key is None:
            return AdminResult(self._llm_usage(f"I do not know a model setting called {head!r}."))
        if not rest:
            return AdminResult(self._llm_usage(f"Say what to set `{head}` to."))

        value = rest.split()[0] if key != "llm.model" else rest
        if key == "llm.base_url" and not value.lower().startswith(("http://", "https://")):
            return AdminResult(
                f"❌ A base URL has to start with http:// or https:// — got {value!r}."
            )

        save_local_override(self._settings.data_dir, key, value)
        self._apply_llm(key, value)
        self._reload_llm()
        secret = key in SECRET_KEYS
        shown = mask(value) if secret else f"`{value}`"
        log.warning("admin.llm_changed", key=key, secret=secret)

        message = [f"✅ {key.split('.')[1]} → {shown}", "In force from the next run."]
        if secret:
            message.append("_I deleted your message so the key is not left in this chat._")
        if key == "llm.base_url":
            message.append(
                "⚠️ Every message I process now goes to that endpoint. Only point this "
                "at something you control."
            )
        return AdminResult("\n".join(message), changed=True, contained_secret=secret)

    def _apply_llm(self, key: str, value: str) -> None:
        from pydantic import SecretStr

        from tgagent.observability.redaction import secret_registry

        field = key.split(".", 1)[1]
        if key in SECRET_KEYS:
            secret_registry.register(value)
            setattr(self._settings.llm, field, SecretStr(value))
        else:
            setattr(self._settings.llm, field, value)

    def _reload_llm(self) -> None:
        if self._on_llm_changed is not None:
            self._on_llm_changed()

    def _llm_overview(self) -> str:
        llm = self._settings.llm
        key = llm.api_key.get_secret_value() if llm.api_key else ""
        lines = [
            "**Model**",
            f"· provider: **{llm.provider}**",
            f"· model: **{llm.model}**",
            f"· api key: {mask(key) if key else '_not set_'}",
            f"· base url: {llm.base_url or '_provider default_'}",
        ]
        if overridden := describe_local_overrides(self._settings.data_dir):
            lines.append("\n_Set from a chat: " + ", ".join(sorted(overridden)) + "_")
        trigger = self._trigger()
        lines.append(f"\n`{trigger} llm model <name>` · `{trigger} llm key <key>` · see help")
        return "\n".join(lines)

    def _llm_usage(self, problem: str) -> str:
        trigger = self._trigger()
        return (
            f"{problem}\n\n"
            f"`{trigger} llm` — what is configured now\n"
            f"`{trigger} llm model claude-opus-5`\n"
            f"`{trigger} llm provider anthropic`\n"
            f"`{trigger} llm key sk-…` — I delete the message afterwards\n"
            f"`{trigger} llm url https://…` — an OpenAI-compatible endpoint\n"
            f"`{trigger} llm reset model` — back to the environment's value"
        )

    # --------------------------------------------------------------- shared ---
    def _trigger(self) -> str:
        return self._settings.control.trigger

    def describe_for_log(self) -> dict[str, Any]:
        """A snapshot for the audit trail. Never includes a secret."""
        return {
            "provider": self._settings.llm.provider,
            "model": self._settings.llm.model,
            "chat_policy": sorted(load_chat_overrides(self._settings.data_dir)),
            "chat_settings": sorted(load_local_overrides(self._settings.data_dir)),
            "policy_file": str(chat_policy_path(self._settings.data_dir)),
        }


__all__ = ["AdminResult", "RuntimeAdmin"]
