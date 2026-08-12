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
    OperationRequest,
    PermissionEngine,
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
