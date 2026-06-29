"""Tests for the runbook config contract + content hash (STAGE-009-001).

Branch-coverage map:
- AlertMatcher._require_some_predicate raise  → test_neither_predicate_rejected
- AlertMatcher._require_some_predicate pass   → test_alertname_only_valid, test_labels_only_valid,
                                                 TestRunbookConfigValid.*
- ScopedCapabilities._require_some_scope raise → test_no_scope_rejected, test_empty_object_rejected
- ScopedCapabilities._require_some_scope pass  → test_docker_only_valid, test_ssh_only_valid,
                                                  TestRunbookConfigValid.*
- RunbookConfig.load_from_path isinstance False → test_non_mapping_root_rejected
- RunbookConfig.load_from_path ValidationError  → test_invalid_content_wrapped_with_path
- RunbookConfig.load_from_path success          → test_happy_path
- Field constraints (min_length, ge, pattern, Literal, list min_length, extra=forbid):
  covered by TestRunbookConfigInvalid, TestAlertMatcher, TestScopedCapabilities
- compute_runbook_content_hash linear path     → every TestContentHash test
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.runbooks import (
    AlertMatcher,
    RiskTag,
    RunbookConfig,
    ScopedCapabilities,
    compute_runbook_content_hash,
)

# A minimal valid config dict reused across tests.
_VALID: dict[str, object] = {
    "name": "restart-nginx",
    "match_patterns": [{"alertname": "NginxDown"}],
    "rate_limit_per_hour": 3,
    "cooldown_seconds": 600,
    "scoped_capabilities": {"docker": {"container": "nginx"}},
}


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "runbook.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestRunbookConfigValid:
    def test_full_config_parses(self) -> None:
        """All fields present, explicit non-default values round-trip correctly."""
        cfg = RunbookConfig.model_validate(
            {
                "runbook": 1,
                "name": "restart-nginx",
                "match_patterns": [
                    {"alertname": "NginxDown", "labels": {"job": "nginx"}},
                    {"labels": {"severity": "critical"}},
                ],
                "risk_tag": "safe",
                "dry_run_required": False,
                "rate_limit_per_hour": 5,
                "cooldown_seconds": 300,
                "scoped_capabilities": {
                    "docker": {"container": "nginx", "allowed_actions": ["restart"]},
                    "ssh": {"target_id": "udm"},
                    "egress": ["https://example.test"],
                },
            }
        )
        assert cfg.name == "restart-nginx"
        assert cfg.risk_tag is RiskTag.SAFE
        assert cfg.dry_run_required is False
        expected_match_pattern_count = 2
        assert len(cfg.match_patterns) == expected_match_pattern_count
        assert cfg.scoped_capabilities.ssh is not None
        assert cfg.scoped_capabilities.ssh.target_id == "udm"

    def test_conservative_defaults_applied(self) -> None:
        """Omitting risk_tag/dry_run_required applies RISKY and True defaults."""
        cfg = RunbookConfig.model_validate(_VALID)
        assert cfg.runbook == 1
        assert cfg.risk_tag is RiskTag.RISKY
        assert cfg.dry_run_required is True
        assert cfg.scoped_capabilities.docker is not None
        assert cfg.scoped_capabilities.docker.allowed_actions == []

    def test_ssh_only_scope_valid(self) -> None:
        """ssh-only scoped_capabilities (no docker) is accepted."""
        data = dict(_VALID)
        data["scoped_capabilities"] = {"ssh": {"target_id": "udm"}}
        cfg = RunbookConfig.model_validate(data)
        assert cfg.scoped_capabilities.docker is None
        assert cfg.scoped_capabilities.ssh is not None

    def test_zero_rate_limit_valid(self) -> None:
        """rate_limit_per_hour=0 is accepted (ge=0)."""
        data = dict(_VALID)
        data["rate_limit_per_hour"] = 0
        cfg = RunbookConfig.model_validate(data)
        assert cfg.rate_limit_per_hour == 0

    def test_zero_cooldown_valid(self) -> None:
        """cooldown_seconds=0 is accepted (ge=0)."""
        data = dict(_VALID)
        data["cooldown_seconds"] = 0
        cfg = RunbookConfig.model_validate(data)
        assert cfg.cooldown_seconds == 0


class TestRunbookConfigInvalid:
    def test_extra_field_rejected(self) -> None:
        """extra='forbid': unknown top-level key raises ValueError."""
        data = dict(_VALID)
        data["bogus"] = 1
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_missing_match_patterns_rejected(self) -> None:
        """match_patterns is required; omitting it raises ValueError."""
        data = dict(_VALID)
        del data["match_patterns"]
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_empty_match_patterns_rejected(self) -> None:
        """match_patterns=[] violates min_length=1."""
        data = dict(_VALID)
        data["match_patterns"] = []
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_missing_rate_limit_rejected(self) -> None:
        """rate_limit_per_hour is required; omitting it raises ValueError."""
        data = dict(_VALID)
        del data["rate_limit_per_hour"]
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_negative_rate_limit_rejected(self) -> None:
        """rate_limit_per_hour=-1 violates ge=0."""
        data = dict(_VALID)
        data["rate_limit_per_hour"] = -1
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_missing_cooldown_rejected(self) -> None:
        """cooldown_seconds is required; omitting it raises ValueError."""
        data = dict(_VALID)
        del data["cooldown_seconds"]
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_negative_cooldown_rejected(self) -> None:
        """cooldown_seconds=-5 violates ge=0."""
        data = dict(_VALID)
        data["cooldown_seconds"] = -5
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_missing_scoped_capabilities_rejected(self) -> None:
        """scoped_capabilities is required; omitting it raises ValueError."""
        data = dict(_VALID)
        del data["scoped_capabilities"]
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_bad_name_pattern_rejected(self) -> None:
        """Name with uppercase/spaces/punctuation violates RUNBOOK_NAME_PATTERN."""
        data = dict(_VALID)
        data["name"] = "Bad Name!"
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_wrong_schema_version_rejected(self) -> None:
        """runbook=2 is not Literal[1]; raises ValueError."""
        data = dict(_VALID)
        data["runbook"] = 2
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)

    def test_name_too_short_rejected(self) -> None:
        """Name 'ab' (2 chars) violates {2,63} minimum — pattern requires ≥3 total chars."""
        data = dict(_VALID)
        data["name"] = "ab"
        with pytest.raises(ValueError):
            RunbookConfig.model_validate(data)


class TestAlertMatcher:
    def test_alertname_only_valid(self) -> None:
        """alertname present, no labels → _require_some_predicate pass branch."""
        m = AlertMatcher.model_validate({"alertname": "X"})
        assert m.alertname == "X"
        assert m.labels == {}

    def test_labels_only_valid(self) -> None:
        """labels present, no alertname → _require_some_predicate pass branch."""
        m = AlertMatcher.model_validate({"labels": {"job": "nginx"}})
        assert m.alertname is None
        assert m.labels == {"job": "nginx"}

    def test_both_alertname_and_labels_valid(self) -> None:
        """Both alertname and labels → pass branch."""
        m = AlertMatcher.model_validate({"alertname": "NginxDown", "labels": {"env": "prod"}})
        assert m.alertname == "NginxDown"
        assert m.labels == {"env": "prod"}

    def test_neither_predicate_rejected(self) -> None:
        """Empty AlertMatcher → _require_some_predicate raise branch."""
        with pytest.raises(ValueError):
            AlertMatcher.model_validate({})

    def test_empty_alertname_string_rejected(self) -> None:
        """alertname='' violates min_length=1."""
        with pytest.raises(ValueError):
            AlertMatcher.model_validate({"alertname": ""})

    def test_extra_field_rejected(self) -> None:
        """extra='forbid': unknown key in AlertMatcher raises ValueError."""
        with pytest.raises(ValueError):
            AlertMatcher.model_validate({"alertname": "X", "bogus": 1})


class TestScopedCapabilities:
    def test_docker_only_valid(self) -> None:
        """docker declared, ssh=None → _require_some_scope pass branch."""
        sc = ScopedCapabilities.model_validate({"docker": {"container": "nginx"}})
        assert sc.docker is not None
        assert sc.ssh is None

    def test_ssh_only_valid(self) -> None:
        """ssh declared, docker=None → _require_some_scope pass branch."""
        sc = ScopedCapabilities.model_validate({"ssh": {"target_id": "udm"}})
        assert sc.ssh is not None
        assert sc.docker is None

    def test_both_docker_and_ssh_valid(self) -> None:
        """Both docker and ssh declared → pass branch."""
        sc = ScopedCapabilities.model_validate(
            {"docker": {"container": "nginx"}, "ssh": {"target_id": "udm"}}
        )
        assert sc.docker is not None
        assert sc.ssh is not None

    def test_no_scope_rejected(self) -> None:
        """egress only (no docker/ssh) → _require_some_scope raise branch."""
        with pytest.raises(ValueError):
            ScopedCapabilities.model_validate({"egress": ["x"]})

    def test_empty_object_rejected(self) -> None:
        """No fields at all → _require_some_scope raise branch."""
        with pytest.raises(ValueError):
            ScopedCapabilities.model_validate({})

    def test_docker_empty_container_rejected(self) -> None:
        """DockerCapability.container='' violates min_length=1."""
        with pytest.raises(ValueError):
            ScopedCapabilities.model_validate({"docker": {"container": ""}})

    def test_ssh_empty_target_id_rejected(self) -> None:
        """SshCapability.target_id='' violates min_length=1."""
        with pytest.raises(ValueError):
            ScopedCapabilities.model_validate({"ssh": {"target_id": ""}})

    def test_egress_list_parses(self) -> None:
        """egress list alongside docker is stored correctly."""
        sc = ScopedCapabilities.model_validate(
            {"docker": {"container": "nginx"}, "egress": ["https://api.example.com"]}
        )
        assert sc.egress == ["https://api.example.com"]

    def test_egress_empty_string_rejected(self) -> None:
        """egress entry '' violates min_length=1 on the annotated list element."""
        with pytest.raises(ValueError):
            ScopedCapabilities.model_validate(
                {"docker": {"container": "nginx"}, "egress": ["valid-host", ""]}
            )


class TestLoadFromPath:
    def test_happy_path(self, tmp_path: Path) -> None:
        """Valid YAML mapping → parsed RunbookConfig (isinstance True + success branch)."""
        body = """\
name: restart-nginx
match_patterns:
  - alertname: NginxDown
rate_limit_per_hour: 3
cooldown_seconds: 600
scoped_capabilities:
  docker:
    container: nginx
    allowed_actions: [restart]
"""
        path = _write(tmp_path, body)
        cfg = RunbookConfig.load_from_path(path)
        assert cfg.name == "restart-nginx"
        assert cfg.scoped_capabilities.docker is not None
        assert cfg.scoped_capabilities.docker.container == "nginx"

    def test_non_mapping_root_rejected(self, tmp_path: Path) -> None:
        """YAML root is a list → isinstance(data, dict) False branch raises ValueError."""
        path = _write(tmp_path, "- a\n- b\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            RunbookConfig.load_from_path(path)

    def test_scalar_root_rejected(self, tmp_path: Path) -> None:
        """YAML root is a scalar string → isinstance guard raise branch."""
        path = _write(tmp_path, "just a string\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            RunbookConfig.load_from_path(path)

    def test_invalid_content_wrapped_with_path(self, tmp_path: Path) -> None:
        """Valid mapping but missing required fields → ValidationError wrapped as ValueError."""
        body = "name: restart-nginx\n"  # missing rate_limit_per_hour, cooldown_seconds, etc.
        path = _write(tmp_path, body)
        with pytest.raises(ValueError, match="is invalid"):
            RunbookConfig.load_from_path(path)

    def test_error_message_contains_path(self, tmp_path: Path) -> None:
        """ValueError from non-mapping root contains the file path for operator context."""
        path = _write(tmp_path, "- item\n")
        with pytest.raises(ValueError, match=str(path)):
            RunbookConfig.load_from_path(path)


class TestContentHash:
    def test_deterministic_same_config(self) -> None:
        """Same config built twice → identical hash (determinism)."""
        a = RunbookConfig.model_validate(_VALID)
        b = RunbookConfig.model_validate(_VALID)
        assert compute_runbook_content_hash(a) == compute_runbook_content_hash(b)

    def test_hash_is_64_hex(self) -> None:
        """Hash is a 64-character lowercase hex string (SHA256)."""
        h = compute_runbook_content_hash(RunbookConfig.model_validate(_VALID))
        sha256_hex_length = 64
        assert len(h) == sha256_hex_length
        assert all(c in "0123456789abcdef" for c in h)

    def test_yaml_formatting_invariant(self, tmp_path: Path) -> None:
        """Two YAML files that differ only in key order/whitespace hash identically."""
        body1 = """\
name: restart-nginx
match_patterns:
  - alertname: NginxDown
rate_limit_per_hour: 3
cooldown_seconds: 600
scoped_capabilities:
  docker:
    container: nginx
"""
        body2 = """\
cooldown_seconds: 600
rate_limit_per_hour: 3
scoped_capabilities:
  docker: {container: nginx}
match_patterns: [{alertname: NginxDown}]
name: restart-nginx
"""
        cfg1 = RunbookConfig.load_from_path(_write(tmp_path, body1))
        p2 = tmp_path / "rb2.yaml"
        p2.write_text(body2, encoding="utf-8")
        cfg2 = RunbookConfig.load_from_path(p2)
        assert compute_runbook_content_hash(cfg1) == compute_runbook_content_hash(cfg2)

    def test_different_config_different_hash(self) -> None:
        """Semantically different configs produce different hashes."""
        a = RunbookConfig.model_validate(_VALID)
        other = dict(_VALID)
        other["cooldown_seconds"] = 999
        b = RunbookConfig.model_validate(other)
        assert compute_runbook_content_hash(a) != compute_runbook_content_hash(b)
