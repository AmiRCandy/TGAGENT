"""Configuration and permission-policy loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tgagent.config.policy import apply_policy, load_policy_file, resolve_permissions
from tgagent.config.settings import PermissionSettings, Settings, load_settings
from tgagent.errors import ConfigError, PolicyError
from tgagent.risk import PolicyDecision, RiskTier


class TestSettings:
    def test_defaults_are_conservative(self) -> None:
        settings = Settings()
        defaults = settings.permissions.defaults
        assert defaults[RiskTier.READ_ONLY] is PolicyDecision.ALLOW
        assert defaults[RiskTier.EXTERNALLY_VISIBLE] is PolicyDecision.CONFIRM
        assert defaults[RiskTier.DESTRUCTIVE] is PolicyDecision.CONFIRM
        assert defaults[RiskTier.ACCOUNT_SECURITY] is PolicyDecision.DENY
        # An unattended run must not silently do what a person would be asked about.
        assert settings.permissions.non_interactive_decision is PolicyDecision.DENY

    def test_sampling_parameters_default_to_unset(self) -> None:
        # Several current models reject temperature/top_p outright, so they must
        # only be sent when explicitly configured.
        settings = Settings()
        assert settings.llm.temperature is None
        assert settings.llm.top_p is None

    def test_paths_resolve_under_data_dir(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path)
        assert settings.storage.database_path == (tmp_path / "tgagent.db").resolve()
        assert settings.telegram.session_dir == (tmp_path / "sessions").resolve()
        assert settings.session_path.name == "tgagent.session"

    def test_env_overrides_nested_values(
        self, monkeypatch: pytest.MonkeyPatch, isolated_env: None
    ) -> None:
        monkeypatch.setenv("TGAGENT_LLM__MODEL", "some-other-model")
        monkeypatch.setenv("TGAGENT_AGENT__MAX_STEPS", "7")
        monkeypatch.setenv("TGAGENT_SANDBOX__BACKEND", "docker")
        settings = load_settings()
        assert settings.llm.model == "some-other-model"
        assert settings.agent.max_steps == 7
        assert settings.sandbox.backend == "docker"

    @pytest.mark.parametrize("phone", ["+15551234567", "+44 7700 900123", "+81-90-1234-5678"])
    def test_valid_phone_numbers(self, phone: str) -> None:
        assert Settings(telegram={"phone": phone}).telegram.phone.startswith("+")

    @pytest.mark.parametrize("phone", ["5551234567", "+abc", "+1", "not a phone"])
    def test_invalid_phone_numbers_rejected(self, phone: str) -> None:
        with pytest.raises(ValidationError):
            Settings(telegram={"phone": phone})

    def test_require_telegram_explains_how_to_fix(self) -> None:
        settings = Settings()
        with pytest.raises(ConfigError) as caught:
            settings.require_telegram()
        assert "TGAGENT_TELEGRAM__API_ID" in str(caught.value)

    def test_secrets_are_not_in_repr(self) -> None:
        settings = Settings(telegram={"api_id": 1, "api_hash": "s3cr3t-hash-value"})
        assert "s3cr3t-hash-value" not in repr(settings)
        assert "s3cr3t-hash-value" not in str(settings.model_dump())

    def test_limits_are_bounded(self) -> None:
        with pytest.raises(ValidationError):
            Settings(agent={"max_steps": 0})
        with pytest.raises(ValidationError):
            Settings(agent={"compaction_threshold": 1.5})


class TestPolicyFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text(
            """
            read_only_mode: true
            max_outbound_per_run: 3
            defaults:
              externally_visible: deny
              destructive: deny
            method_overrides:
              messages.SendMessage: deny
              get_messages: allow
            chat_allowlist: ["@alex"]
            """,
            encoding="utf-8",
        )
        merged = apply_policy(PermissionSettings(), load_policy_file(path), source=str(path))

        assert merged.read_only_mode is True
        assert merged.max_outbound_per_run == 3
        assert merged.defaults[RiskTier.EXTERNALLY_VISIBLE] is PolicyDecision.DENY
        assert merged.method_overrides["messages.SendMessage"] is PolicyDecision.DENY
        assert merged.chat_allowlist == ["@alex"]
        # Untouched tiers keep their defaults.
        assert merged.defaults[RiskTier.READ_ONLY] is PolicyDecision.ALLOW

    def test_unknown_key_is_an_error_not_a_silent_noop(self, tmp_path: Path) -> None:
        # A typo in a security policy that does nothing is the failure worth
        # preventing, so this must be loud.
        path = tmp_path / "policy.yaml"
        path.write_text("read_only_mod: true\n", encoding="utf-8")
        with pytest.raises(PolicyError, match="Unknown key"):
            apply_policy(PermissionSettings(), load_policy_file(path), source=str(path))

    def test_unknown_tier_rejected(self) -> None:
        with pytest.raises(PolicyError, match="Unknown risk tier"):
            apply_policy(PermissionSettings(), {"defaults": {"nonsense": "allow"}}, source="t")

    def test_unknown_decision_rejected(self) -> None:
        with pytest.raises(PolicyError, match="Unknown decision"):
            apply_policy(PermissionSettings(), {"defaults": {"read_only": "maybe"}}, source="t")

    def test_malformed_yaml_reports_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text("defaults: [unclosed\n", encoding="utf-8")
        with pytest.raises(PolicyError, match="not valid YAML"):
            load_policy_file(path)

    def test_non_mapping_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(PolicyError, match="mapping"):
            load_policy_file(path)

    def test_empty_file_is_a_no_op(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text("", encoding="utf-8")
        assert load_policy_file(path) == {}

    def test_missing_configured_file_is_an_error(self, tmp_path: Path) -> None:
        permissions = PermissionSettings(policy_file=tmp_path / "nope.yaml")
        with pytest.raises(PolicyError, match="does not exist"):
            resolve_permissions(permissions)

    def test_no_policy_file_passes_through(self) -> None:
        base = PermissionSettings()
        assert resolve_permissions(base) is base
