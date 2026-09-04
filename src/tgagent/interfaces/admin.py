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
        on_plugins_changed: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._permissions = permissions
        #: Rebuilds the tool registry after a plugin changes, so `plugin add`
        #: does not need a restart to mean anything.
        self._on_plugins_changed = on_plugins_changed
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

    # -------------------------------------------------------------- plugins ---
    async def plugins(self, argument: str) -> AdminResult:
        """Handle ``plugin``, ``plugin add|remove|on|off|set|info``.

        Async because installing one clones a repository. Owner-only for the same
        reason as everything else here, and more so: a plugin is code that will
        run with this account's credentials.
        """
        from tgagent.plugins import PluginError, PluginState

        words = argument.split()
        state = PluginState(self._settings.data_dir)
        verb = words[0].casefold() if words else "list"
        rest = words[1:]

        try:
            if verb in ("", "list", "ls", "status"):
                return AdminResult(await self._plugin_list(state))
            if verb in ("add", "install"):
                return await self._plugin_add(state, rest)
            if verb in ("remove", "rm", "uninstall", "delete"):
                return await self._plugin_remove(state, rest)
            if verb in ("on", "enable"):
                return await self._plugin_toggle(state, rest, enabled=True)
            if verb in ("off", "disable"):
                return await self._plugin_toggle(state, rest, enabled=False)
            if verb == "set":
                return await self._plugin_set(state, rest)
            if verb == "info":
                return AdminResult(await self._plugin_info(state, rest))
        except PluginError as exc:
            return AdminResult(f"\u274c {exc.user_message}")

        return AdminResult(self._plugin_usage(f"I do not know what {verb!r} means here."))

    async def _plugin_list(self, state: Any) -> str:
        from tgagent.plugins import load_plugins

        _tools, report = load_plugins(self._settings)
        if not report:
            return "No plugins are available in this deployment."

        trigger = self._trigger()
        lines = ["**Plugins**"]
        for entry in report:
            manifest, record = entry.manifest, entry.installed
            mark = "\u2705" if entry.ok else ("\u23f8" if not record.enabled else "\u26a0\ufe0f")
            origin = "built in" if record.builtin else record.source
            lines.append(f"{mark} **{manifest.name}** `{manifest.version}` \u00b7 {origin}")
            lines.append(f"   {manifest.description}")
            if entry.ok:
                lines.append("   tools: " + ", ".join(f"`{tool.name}`" for tool in entry.tools))
            else:
                lines.append(f"   _{entry.error}_")
        lines.append(
            f"\n`{trigger} plugin add <url>` \u00b7 `off <name>` \u00b7 `on <name>` \u00b7 "
            f"`set <name> <key> <value>` \u00b7 `info <name>`"
        )
        return "\n".join(lines)

    async def _plugin_add(self, state: Any, words: list[str]) -> AdminResult:
        from tgagent.plugins import install

        if not self._settings.plugins.allow_install:
            return AdminResult(
                "\u274c Installing plugins is switched off here "
                "(`plugins.allow_install`), so the set of plugins is whatever is "
                "already on disk."
            )
        if not words:
            return AdminResult(self._plugin_usage("Give me the plugin's git URL."))

        settings = self._settings.plugins
        outcome = await install(
            words[0],
            state=state,
            trusted_hosts=settings.trusted_hosts,
            max_installed=settings.max_installed,
            ref=words[1] if len(words) > 1 else "",
        )
        report = self._reload_plugins()
        loaded = next((e for e in report if e.manifest.name == outcome.manifest.name), None)

        lines = [
            f"\u2705 Installed **{outcome.manifest.name}** `{outcome.manifest.version}`"
            + (" (replacing what was there)" if outcome.replaced else ""),
            f"   {outcome.manifest.description}",
            f"   commit `{outcome.record.ref[:12] or 'unknown'}`",
        ]
        if loaded is not None and loaded.ok:
            lines.append("   tools: " + ", ".join(f"`{t.name}`" for t in loaded.tools))
            lines.append("\nAvailable now — no restart needed.")
        elif loaded is not None:
            lines.append(f"   \u26a0\ufe0f not loaded: {loaded.error}")
        lines.append(
            "\n\u26a0\ufe0f This code now runs inside the agent, with the same access to "
            "your account and keys as the agent itself. `plugin off` stops it."
        )
        return AdminResult("\n".join(lines), changed=True)

    async def _plugin_remove(self, state: Any, words: list[str]) -> AdminResult:
        from tgagent.plugins import remove

        if not words:
            return AdminResult(self._plugin_usage("Which plugin?"))
        name = words[0]
        deleted = remove(name, state=state)
        self._reload_plugins()
        if deleted:
            return AdminResult(f"\u2705 Removed **{name}** and deleted its files.", changed=True)
        return AdminResult(
            f"\u2705 **{name}** ships with tgagent, so it is switched off rather than "
            f"deleted. `{self._trigger()} plugin on {name}` brings it back.",
            changed=True,
        )

    async def _plugin_toggle(self, state: Any, words: list[str], *, enabled: bool) -> AdminResult:
        from tgagent.plugins import ensure_record

        if not words:
            return AdminResult(self._plugin_usage("Which plugin?"))
        name = words[0]
        # A built-in with no row yet is "default", not "missing".
        ensure_record(state, name, settings=self._settings)
        state.set_enabled(name, enabled)
        report = self._reload_plugins()
        entry = next((e for e in report if e.manifest.name == name), None)

        if not enabled:
            return AdminResult(
                f"\u2705 **{name}** is off. Its tools are gone from my list.", changed=True
            )
        if entry is not None and entry.ok:
            tools = ", ".join(f"`{t.name}`" for t in entry.tools)
            return AdminResult(f"\u2705 **{name}** is on. Tools: {tools}", changed=True)
        detail = entry.error if entry is not None else "it is not installed"
        return AdminResult(f"\u26a0\ufe0f **{name}** is on but not loading: {detail}", changed=True)

    async def _plugin_set(self, state: Any, words: list[str]) -> AdminResult:
        from tgagent.plugins import ensure_record

        if len(words) < 3:
            return AdminResult(
                self._plugin_usage(
                    f"Say which plugin, which key, and the value: "
                    f"`{self._trigger()} plugin set web-search api_key sk-...`"
                )
            )
        name, key, value = words[0], words[1], " ".join(words[2:])
        ensure_record(state, name, settings=self._settings)
        state.set_config(name, {key: _coerce(value)})
        self._reload_plugins()

        secret = any(hint in key.lower() for hint in ("key", "token", "secret", "password"))
        shown = mask(value) if secret else f"`{value}`"
        message = [f"\u2705 **{name}**: {key} \u2192 {shown}"]
        if secret:
            message.append("_I deleted your message so the key is not left in this chat._")
        return AdminResult("\n".join(message), changed=True, contained_secret=secret)

    async def _plugin_info(self, state: Any, words: list[str]) -> str:
        from tgagent.plugins import load_plugins, missing_requirements

        if not words:
            return self._plugin_usage("Which plugin?")
        name = words[0]
        _tools, report = load_plugins(self._settings)
        entry = next((e for e in report if e.manifest.name == name), None)
        if entry is None:
            return f"No plugin named {name!r}. `{self._trigger()} plugin list` shows them."

        manifest, record = entry.manifest, entry.installed
        lines = [
            f"**{manifest.name}** `{manifest.version}`",
            manifest.description or "_no description_",
            f"\u00b7 source: {'built in' if record.builtin else record.source}",
            f"\u00b7 enabled: {'yes' if record.enabled else 'no'}",
            f"\u00b7 status: {'loaded' if entry.ok else entry.error}",
        ]
        if record.ref:
            lines.append(f"\u00b7 commit: `{record.ref[:12]}`")
        if manifest.tools:
            lines.append("\u00b7 tools: " + ", ".join(f"`{t}`" for t in manifest.tools))
        if manifest.requires:
            missing = missing_requirements(manifest)
            state_text = "all present" if not missing else f"missing {', '.join(missing)}"
            lines.append(f"\u00b7 requires: {', '.join(manifest.requires)} \u2014 {state_text}")
        if record.config:
            shown = {
                key: (mask(str(value)) if _is_secret(key) else value)
                for key, value in record.config.items()
            }
            lines.append(f"\u00b7 config: {shown}")
        if manifest.homepage:
            lines.append(f"\u00b7 {manifest.homepage}")
        return "\n".join(lines)

    def _plugin_usage(self, problem: str) -> str:
        trigger = self._trigger()
        return (
            f"{problem}\n\n"
            f"`{trigger} plugin list` \u2014 what is installed, and what is loading\n"
            f"`{trigger} plugin add owner/repo` \u2014 install from GitHub\n"
            f"`{trigger} plugin off web-search` \u2014 stop it without deleting it\n"
            f"`{trigger} plugin on web-search` \u2014 start it again\n"
            f"`{trigger} plugin set web-search api_key sk-\u2026` \u2014 configure it\n"
            f"`{trigger} plugin remove <name>` \u2014 delete it\n"
            f"`{trigger} plugin info <name>` \u2014 everything about one\n\n"
            f"A plugin runs inside the agent with your account's access. Install only "
            f"what you would trust with the account itself."
        )

    def _reload_plugins(self) -> list[Any]:
        """Rebuild the live tool list, and report what loading found.

        The hook's job is to update the registry in the running process; the
        report always comes from the loader. Reading the answer out of the hook's
        return value instead made an un-wired hook look like a failed plugin —
        "is on but not installed" about a plugin that was plainly installed.
        """
        from tgagent.plugins import load_plugins

        if self._on_plugins_changed is not None:
            self._on_plugins_changed()
        _tools, report = load_plugins(self._settings)
        return list(report)

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


def _is_secret(key: str) -> bool:
    return any(hint in key.lower() for hint in ("key", "token", "secret", "password"))


def _coerce(value: str) -> Any:
    """Turn a chat-typed value into the obvious type.

    `max_megabytes 50` should store 50, not "50" — a plugin reading its own
    config should not have to guess whether the operator typed it or the manifest
    declared it.
    """
    text = value.strip()
    if text.lower() in ("true", "yes", "on"):
        return True
    if text.lower() in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
