"""Risk classification and the permission engine.

These are the tests that matter most: if classification or authorisation is
wrong, everything else in the security model is decoration.
"""

from __future__ import annotations

import pytest

from tgagent.config.settings import PermissionSettings
from tgagent.errors import PermissionDenied
from tgagent.risk import PolicyDecision, RiskTier
from tgagent.security.permissions import (
    _ACCOUNT_SECURITY_METHODS,
    _DESTRUCTIVE_METHODS,
    _EXTERNALLY_VISIBLE_METHODS,
    _READ_ONLY_METHODS,
    _REVERSIBLE_METHODS,
    OperationRequest,
    PermissionEngine,
    canonical_method_key,
    classify,
    normalise_method,
)


class TestClassification:
    @pytest.mark.parametrize(
        "method",
        [
            "messages.GetHistory",
            "get_messages",
            "messages.Search",
            "get_dialogs",
            "channels.GetParticipants",
            "users.GetFullUser",
            "contacts.ResolveUsername",
            "iter_messages",
            "get_entity",
            "messages.GetSearchCounters",
        ],
    )
    def test_reads(self, method: str) -> None:
        assert classify(method) is RiskTier.READ_ONLY

    @pytest.mark.parametrize(
        "method",
        ["messages.ReadHistory", "download_media", "messages.SaveDraft", "mark_read"],
    )
    def test_reversible(self, method: str) -> None:
        assert classify(method) is RiskTier.REVERSIBLE

    @pytest.mark.parametrize(
        "method",
        [
            "messages.SendMessage",
            "send_message",
            "messages.EditMessage",
            "messages.ForwardMessages",
            "channels.JoinChannel",
            "messages.SendMedia",
            "messages.SendReaction",
            "upload_file",
            "contacts.AddContact",
        ],
    )
    def test_externally_visible(self, method: str) -> None:
        assert classify(method) is RiskTier.EXTERNALLY_VISIBLE

    @pytest.mark.parametrize(
        "method",
        [
            "messages.DeleteMessages",
            "messages.DeleteHistory",
            "delete_messages",
            "channels.LeaveChannel",
            "channels.EditBanned",
            "messages.DeleteChat",
            "contacts.Block",
            "delete_dialog",
        ],
    )
    def test_destructive(self, method: str) -> None:
        assert classify(method) is RiskTier.DESTRUCTIVE

    @pytest.mark.parametrize(
        "method",
        [
            "account.UpdatePasswordSettings",
            "auth.LogOut",
            "account.ResetAuthorization",
            "account.DeleteAccount",
            "auth.ExportAuthorization",
            "account.UpdatePrivacy",
            "edit_2fa",
            "log_out",
        ],
    )
    def test_account_security(self, method: str) -> None:
        assert classify(method) is RiskTier.ACCOUNT_SECURITY

    def test_unknown_write_shaped_method_defaults_to_destructive(self) -> None:
        # The property that matters: a future Telethon release cannot introduce
        # a method that executes without a decision.
        assert classify("messages.SomeBrandNewThing") is RiskTier.DESTRUCTIVE
        assert classify("obliterate_everything") is RiskTier.DESTRUCTIVE

    def test_unknown_read_shaped_method_is_read_only(self) -> None:
        assert classify("messages.GetSomethingNew") is RiskTier.READ_ONLY
        assert classify("search_whatever") is RiskTier.READ_ONLY

    def test_classification_is_case_and_suffix_insensitive(self) -> None:
        assert classify("messages.SendMessageRequest") is RiskTier.EXTERNALLY_VISIBLE
        assert classify("MESSAGES.SENDMESSAGE") is RiskTier.EXTERNALLY_VISIBLE

    def test_normalise_method(self) -> None:
        assert normalise_method("messages.SendMessage") == ("messages", "sendmessage")
        assert normalise_method("send_message") == ("", "send_message")
        assert normalise_method("messages.SearchRequest") == ("messages", "search")

    def test_canonical_method_key_folds_the_two_spellings_together(self) -> None:
        assert (
            canonical_method_key("send_message")[1]
            == canonical_method_key("messages.SendMessage")[1]
        )
        # …without folding together methods that are genuinely different.
        assert (
            canonical_method_key("messages.DeleteHistory")[1]
            != (canonical_method_key("delete_messages")[1])
        )
        assert canonical_method_key("block")[1] != canonical_method_key("blockuser")[1]

    def test_a_reversible_ui_toggle_is_not_misread_as_destructive(self) -> None:
        # `toggledialogfilterTags` used to be spelled with a capital, and the rule
        # tables are searched with a lowercased name, so the entry never matched
        # and a trivial UI toggle fell through to the destructive fallback.
        assert classify("messages.ToggleDialogFilterTags") is RiskTier.REVERSIBLE
        assert classify("toggledialogfiltertags") is RiskTier.REVERSIBLE

    def test_no_rule_table_entry_is_unreachable(self) -> None:
        # The general form of the bug above: an entry carrying a capital can never
        # be matched, and it does not fail loudly — it silently misclassifies.
        for table in (
            _ACCOUNT_SECURITY_METHODS,
            _DESTRUCTIVE_METHODS,
            _EXTERNALLY_VISIBLE_METHODS,
            _REVERSIBLE_METHODS,
            _READ_ONLY_METHODS,
        ):
            unreachable = sorted(entry for entry in table if entry != entry.lower())
            assert unreachable == []

    def test_tier_ordering(self) -> None:
        assert RiskTier.DESTRUCTIVE.at_least(RiskTier.EXTERNALLY_VISIBLE)
        assert not RiskTier.READ_ONLY.at_least(RiskTier.REVERSIBLE)
        assert RiskTier.ACCOUNT_SECURITY.at_least(RiskTier.ACCOUNT_SECURITY)


class TestAuthorisation:
    def test_reads_allowed_writes_confirmed_by_default(self) -> None:
        engine = PermissionEngine(PermissionSettings())
        read = engine.authorize(OperationRequest(method="get_messages"), interactive=True)
        write = engine.authorize(OperationRequest(method="send_message"), interactive=True)
        security = engine.authorize(OperationRequest(method="auth.LogOut"), interactive=True)

        assert read.decision is PolicyDecision.ALLOW
        assert write.decision is PolicyDecision.CONFIRM
        assert security.decision is PolicyDecision.DENY

    def test_non_interactive_confirm_falls_back_to_deny(self) -> None:
        engine = PermissionEngine(PermissionSettings())
        result = engine.authorize(OperationRequest(method="send_message"), interactive=False)
        assert result.decision is PolicyDecision.DENY
        assert "no interactive user" in result.reason.lower()

    def test_non_interactive_fallback_is_configurable(self) -> None:
        engine = PermissionEngine(PermissionSettings(non_interactive_decision=PolicyDecision.ALLOW))
        result = engine.authorize(OperationRequest(method="send_message"), interactive=False)
        assert result.decision is PolicyDecision.ALLOW

    def test_read_only_mode_blocks_all_writes(self) -> None:
        engine = PermissionEngine(PermissionSettings(read_only_mode=True))
        assert (
            engine.authorize(OperationRequest(method="get_messages"), interactive=True).decision
            is PolicyDecision.ALLOW
        )
        for method in ("send_message", "delete_messages", "messages.ForwardMessages"):
            result = engine.authorize(OperationRequest(method=method), interactive=True)
            assert result.decision is PolicyDecision.DENY
            assert "read_only_mode" in result.reason

    def test_method_override_beats_tier_default(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"send_message": PolicyDecision.ALLOW})
        )
        result = engine.authorize(OperationRequest(method="send_message"), interactive=True)
        assert result.decision is PolicyDecision.ALLOW

    def test_override_matches_across_naming_styles(self) -> None:
        # A policy written as `messages.SendMessage` must also govern a call the
        # model makes as `send_message`, or the policy is trivially bypassable.
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"messages.SendMessage": PolicyDecision.DENY})
        )
        result = engine.authorize(OperationRequest(method="messages.SendMessage"), interactive=True)
        assert result.decision is PolicyDecision.DENY

    @pytest.mark.parametrize("written_as", ["send_message", "messages.SendMessage"])
    @pytest.mark.parametrize(
        "called_as", ["send_message", "messages.SendMessage", "messages.SendMessageRequest"]
    )
    def test_an_override_governs_either_spelling_of_the_method(
        self, written_as: str, called_as: str
    ) -> None:
        # The gateway exposes both routes to the same operation, so an override
        # written in one spelling has to catch calls made in the other — otherwise
        # the other spelling is a free bypass of the policy.
        engine = PermissionEngine(
            PermissionSettings(method_overrides={written_as: PolicyDecision.DENY})
        )
        result = engine.authorize(OperationRequest(method=called_as), interactive=True)
        assert result.decision is PolicyDecision.DENY

    def test_an_override_does_not_leak_onto_a_different_method(self) -> None:
        # Folding the spellings together must not fold distinct operations
        # together: a grant is the direction that fails open.
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"messages.DeleteHistory": PolicyDecision.ALLOW})
        )
        assert (
            engine.authorize(OperationRequest(method="delete_messages"), interactive=True).decision
            is PolicyDecision.CONFIRM
        )
        # Nor may a grant written for one namespace widen another namespace's
        # same-named request.
        namespaced = PermissionEngine(
            PermissionSettings(method_overrides={"channels.DeleteMessages": PolicyDecision.ALLOW})
        )
        assert (
            namespaced.authorize(
                OperationRequest(method="messages.DeleteMessages"), interactive=True
            ).decision
            is PolicyDecision.CONFIRM
        )

    def test_conflicting_spellings_resolve_to_the_strictest(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                method_overrides={
                    "send_message": PolicyDecision.ALLOW,
                    "messages.SendMessage": PolicyDecision.DENY,
                }
            )
        )
        for method in ("send_message", "messages.SendMessage"):
            result = engine.authorize(OperationRequest(method=method), interactive=True)
            assert result.decision is PolicyDecision.DENY

    def test_a_raw_override_tightens_the_friendly_wrapper_that_issues_it(self) -> None:
        # `delete_dialog` is not a re-spelling of `messages.DeleteHistory`, it is a
        # wrapper that issues it, so no normalisation can bridge the two. Pinning
        # the raw request must still govern the friendly route.
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"messages.DeleteHistory": PolicyDecision.DENY})
        )
        for method in ("messages.DeleteHistory", "delete_dialog"):
            result = engine.authorize(OperationRequest(method=method), interactive=True)
            assert result.decision is PolicyDecision.DENY

    def test_wrapper_matching_can_only_tighten_never_loosen(self) -> None:
        # `delete_dialog` also removes members and leaves channels, so an `allow`
        # written for one of the requests it may issue must not grant the wrapper.
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"messages.DeleteHistory": PolicyDecision.ALLOW})
        )
        assert (
            engine.authorize(OperationRequest(method="delete_dialog"), interactive=True).decision
            is PolicyDecision.CONFIRM
        )

    def test_denylist_blocks_writes_to_a_chat(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                chat_denylist=["@work"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        blocked = engine.authorize(
            OperationRequest(method="send_message", target="@work"), interactive=True
        )
        allowed = engine.authorize(
            OperationRequest(method="send_message", target="@alex"), interactive=True
        )
        assert blocked.decision is PolicyDecision.DENY
        assert allowed.decision is PolicyDecision.ALLOW

    def test_denylist_normalises_the_at_sign_and_case(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                chat_denylist=["@Work"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        result = engine.authorize(
            OperationRequest(method="send_message", target="work"), interactive=True
        )
        assert result.decision is PolicyDecision.DENY

    def test_denylist_does_not_fail_open_on_an_unnameable_target(self) -> None:
        # `contacts.Block(id="@work")`: `id` is not one of the peer argument names
        # `extract_target` knows, so the gateway hands the engine `target=None`.
        # Skipping the denylist there would wave through exactly the write the
        # operator forbade, so an unnameable target has to be a refusal.
        engine = PermissionEngine(
            PermissionSettings(
                chat_denylist=["@work"],
                defaults={RiskTier.DESTRUCTIVE: PolicyDecision.ALLOW},
            )
        )
        result = engine.authorize(
            OperationRequest(method="contacts.Block", arguments={"id": "@work"}), interactive=True
        )
        assert result.decision is PolicyDecision.DENY
        assert "denylist" in result.reason

    @pytest.mark.parametrize(
        "target",
        [
            "@company_announcements",
            "company_announcements",
            "t.me/company_announcements",
            "https://t.me/company_announcements",
            "http://www.t.me/company_announcements/",
            "https://t.me/company_announcements/42",
            "HTTPS://T.ME/Company_Announcements",
        ],
    )
    def test_denylist_matches_a_chat_addressed_by_link(self, target: str) -> None:
        # A `t.me` link is the same reference as an `@name`, so writing one form in
        # the policy must cover a call that uses the other.
        engine = PermissionEngine(
            PermissionSettings(
                chat_denylist=["@company_announcements"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        result = engine.authorize(
            OperationRequest(method="send_message", target=target), interactive=True
        )
        assert result.decision is PolicyDecision.DENY

    def test_link_normalisation_does_not_over_match(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                chat_denylist=["https://t.me/company_announcements"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        assert (
            engine.authorize(
                OperationRequest(method="send_message", target="@company_announcements"),
                interactive=True,
            ).decision
            is PolicyDecision.DENY
        )
        # A different chat, and an invite link that is not a username, stay clear.
        for target in ("@company_announcements_v2", "https://t.me/+company_announcements"):
            assert (
                engine.authorize(
                    OperationRequest(method="send_message", target=target), interactive=True
                ).decision
                is PolicyDecision.ALLOW
            )

    def test_allowlist_restricts_writes(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                chat_allowlist=["@alex"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        assert (
            engine.authorize(
                OperationRequest(method="send_message", target="@alex"), interactive=True
            ).decision
            is PolicyDecision.ALLOW
        )
        assert (
            engine.authorize(
                OperationRequest(method="send_message", target="@stranger"), interactive=True
            ).decision
            is PolicyDecision.DENY
        )

    def test_allowlist_denies_writes_with_no_identifiable_target(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                chat_allowlist=["@alex"],
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        result = engine.authorize(OperationRequest(method="send_message"), interactive=True)
        assert result.decision is PolicyDecision.DENY

    def test_reads_are_not_restricted_by_chat_lists(self) -> None:
        engine = PermissionEngine(PermissionSettings(chat_allowlist=["@alex"]))
        result = engine.authorize(
            OperationRequest(method="get_messages", target="@anyone"), interactive=True
        )
        assert result.decision is PolicyDecision.ALLOW

    def test_outbound_budget_is_enforced_per_run(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(
                max_outbound_per_run=2,
                defaults={RiskTier.EXTERNALLY_VISIBLE: PolicyDecision.ALLOW},
            )
        )
        for _ in range(2):
            verdict = engine.authorize(OperationRequest(method="send_message"), interactive=True)
            assert verdict.decision is PolicyDecision.ALLOW
            engine.note_outbound(verdict.risk)

        exhausted = engine.authorize(OperationRequest(method="send_message"), interactive=True)
        assert exhausted.decision is PolicyDecision.DENY
        assert "Per-run limit" in exhausted.reason

        # Reads still work once the write budget is gone.
        assert (
            engine.authorize(OperationRequest(method="get_messages"), interactive=True).decision
            is PolicyDecision.ALLOW
        )

        engine.reset_run_counters()
        assert (
            engine.authorize(OperationRequest(method="send_message"), interactive=True).decision
            is PolicyDecision.ALLOW
        )

    def test_reads_do_not_consume_the_outbound_budget(self) -> None:
        engine = PermissionEngine(PermissionSettings(max_outbound_per_run=1))
        engine.note_outbound(RiskTier.READ_ONLY)
        engine.note_outbound(RiskTier.REVERSIBLE)
        assert engine.outbound_used == 0

    def test_enforce_raises_on_deny(self) -> None:
        engine = PermissionEngine(PermissionSettings())
        request = OperationRequest(method="auth.LogOut")
        verdict = engine.authorize(request, interactive=True)
        with pytest.raises(PermissionDenied) as caught:
            engine.enforce(request, verdict)
        assert caught.value.method == "auth.LogOut"
        assert caught.value.risk == RiskTier.ACCOUNT_SECURITY.value

    def test_argument_digest_is_stable_and_order_independent(self) -> None:
        a = OperationRequest(method="send_message", arguments={"b": 2, "a": 1})
        b = OperationRequest(method="send_message", arguments={"a": 1, "b": 2})
        c = OperationRequest(method="send_message", arguments={"a": 1, "b": 3})
        assert a.argument_digest == b.argument_digest
        assert a.argument_digest != c.argument_digest

    def test_confirmation_prompt_summarises_the_payload(self) -> None:
        engine = PermissionEngine(PermissionSettings())
        verdict = engine.authorize(
            OperationRequest(
                method="send_message",
                arguments={"message": "hello there"},
                target="@alex",
            ),
            interactive=True,
        )
        assert verdict.needs_confirmation
        assert "hello there" in verdict.prompt
        assert "@alex" in verdict.prompt


class TestExplanation:
    """`tgagent config policy <method>` must report what the engine will do.

    An interface that recomputes the lookup drifts the moment the lookup gains a
    rule — and it already had: an override is matched canonically, so one written
    in the friendly spelling governs the raw request and a string comparison in
    the CLI reported "Override: no" about a line that denied the call.
    """

    def test_a_tier_default_is_reported_as_such(self) -> None:
        explanation = PermissionEngine(PermissionSettings()).explain("messages.SendMessage")
        assert explanation.risk is RiskTier.EXTERNALLY_VISIBLE
        assert explanation.decision is PolicyDecision.CONFIRM
        assert not explanation.from_override
        assert explanation.matched_overrides == ()

    def test_an_override_is_reported_under_the_spelling_the_policy_used(self) -> None:
        engine = PermissionEngine(
            PermissionSettings(method_overrides={"send_message": PolicyDecision.DENY})
        )
        explanation = engine.explain("messages.SendMessage")
        assert explanation.decision is PolicyDecision.DENY
        assert explanation.from_override
        assert explanation.matched_overrides == ("send_message",)

    def test_the_explanation_agrees_with_the_verdict(self) -> None:
        """The two must not be able to disagree; that is the whole point."""
        settings = PermissionSettings(
            method_overrides={
                "send_message": PolicyDecision.DENY,
                "messages.ForwardMessages": PolicyDecision.ALLOW,
            }
        )
        engine = PermissionEngine(settings)
        for method in ("send_message", "messages.SendMessage", "forward_messages", "get_messages"):
            explanation = engine.explain(method)
            verdict = engine.authorize(OperationRequest(method=method), interactive=True)
            assert explanation.decision is verdict.decision, method
            assert explanation.risk is verdict.risk, method

    def test_an_unknown_method_explains_the_fail_safe(self) -> None:
        explanation = PermissionEngine(PermissionSettings()).explain("obliterate_everything")
        assert explanation.risk is RiskTier.DESTRUCTIVE
        assert not explanation.from_override
