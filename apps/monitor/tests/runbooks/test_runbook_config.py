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
from homelab_monitor.kernel.runbooks.hashing import HASH_PREFIX, RunbookHashError

_EXPECTED_HASH_LENGTH = 74  # len("v2:sha256:") + 64 hex chars

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


def _write_runbook_folder(
    tmp_path: Path, name: str, files: dict[str, bytes | str] | None = None
) -> Path:
    """Write a minimal valid runbook folder under tmp_path/name; return its path."""
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    default_files: dict[str, bytes | str] = {
        "runbook.yaml": (
            "runbook: 1\n"
            "name: test-runbook\n"
            "match_patterns:\n"
            "  - alertname: HighCPU\n"
            "risk_tag: safe\n"
            "dry_run_required: true\n"
            "rate_limit_per_hour: 5\n"
            "cooldown_seconds: 300\n"
            "scoped_capabilities:\n"
            "  docker:\n"
            "    container: c1\n"
            "    allowed_actions: [restart]\n"
        ),
        "CLAUDE.md": "# Test runbook\n",
    }
    merged = {**default_files, **(files or {})}
    for rel_name, content in merged.items():
        p = folder / rel_name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(content)
    return folder


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
    def test_hash_shape(self, tmp_path: Path) -> None:
        """Hash is HASH_PREFIX + 64 lowercase hex chars."""
        folder = _write_runbook_folder(tmp_path, "rb")
        h = compute_runbook_content_hash(folder)
        assert h.startswith("v2:sha256:")
        assert len(h) == _EXPECTED_HASH_LENGTH
        assert all(c in "0123456789abcdef" for c in h[10:])

    def test_hash_has_v2_prefix(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        assert compute_runbook_content_hash(folder).startswith("v2:sha256:")

    def test_deterministic_across_identical_folders(self, tmp_path: Path) -> None:
        """Same content, different folder paths -> same hash (path not in hash)."""
        a = _write_runbook_folder(tmp_path / "a", "rb")
        b = _write_runbook_folder(tmp_path / "b", "rb")
        assert compute_runbook_content_hash(a) == compute_runbook_content_hash(b)

    def test_mutating_readme_changes_hash(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb", {"README.md": "v1"})
        h1 = compute_runbook_content_hash(folder)
        (folder / "README.md").write_text("v2", encoding="utf-8")
        h2 = compute_runbook_content_hash(folder)
        assert h1 != h2

    def test_adding_sibling_changes_hash(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        h1 = compute_runbook_content_hash(folder)
        (folder / "notes.txt").write_text("hello", encoding="utf-8")
        h2 = compute_runbook_content_hash(folder)
        assert h1 != h2

    def test_removing_sibling_reverses_hash(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        h1 = compute_runbook_content_hash(folder)
        sibling = folder / "notes.txt"
        sibling.write_text("hello", encoding="utf-8")
        compute_runbook_content_hash(folder)
        sibling.unlink()
        h2 = compute_runbook_content_hash(folder)
        assert h1 == h2

    def test_line_ending_canonicalization(self, tmp_path: Path) -> None:
        a = _write_runbook_folder(tmp_path / "a", "rb", {"notes.txt": "line1\nline2\n"})
        b = _write_runbook_folder(tmp_path / "b", "rb", {"notes.txt": "line1\r\nline2\r\n"})
        assert compute_runbook_content_hash(a) == compute_runbook_content_hash(b)

    def test_yaml_bytes_sensitivity_stricter_than_v1(self, tmp_path: Path) -> None:
        """v2 hashes raw bytes of runbook.yaml (not the parsed model) — unlike
        v1, a purely cosmetic edit (trailing comment) now changes the hash.
        This documents the intentionally tighter surface (design non-negotiable #4).
        """
        folder = _write_runbook_folder(tmp_path, "rb")
        h1 = compute_runbook_content_hash(folder)
        yaml_path = folder / "runbook.yaml"
        yaml_path.write_text(
            yaml_path.read_text(encoding="utf-8") + "# trailing comment\n",
            encoding="utf-8",
        )
        h2 = compute_runbook_content_hash(folder)
        assert h1 != h2

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        target = tmp_path / "outside.txt"
        target.write_text("x", encoding="utf-8")
        (folder / "link.txt").symlink_to(target)
        with pytest.raises(RunbookHashError, match="symlink"):
            compute_runbook_content_hash(folder)

    def test_rejects_symlinked_directory(self, tmp_path: Path) -> None:
        """A symlinked SUBDIRECTORY inside the runbook folder is hard-rejected.

        Guards against a future refactor that flips rglob to follow_symlinks=True:
        an unrejected directory symlink could reach files outside the runbook folder
        and silently include them in the hash (folder-isolation escape).
        """
        folder = _write_runbook_folder(tmp_path, "runbook")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "escape.md").write_text("# outside content\n")
        (folder / "sublink").symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(RunbookHashError, match=r"symlink"):
            compute_runbook_content_hash(folder)

    def test_hashes_real_subdirectory(self, tmp_path: Path) -> None:
        """Files under real (non-symlinked) subdirectories ARE part of the hash.

        Runbooks that organize helper scripts under sub/ MUST invalidate the hash
        when those files change. Documents this as intentional behavior.
        """
        # Baseline: folder with just the two required files
        base = _write_runbook_folder(tmp_path, "base")
        base_hash = compute_runbook_content_hash(base)

        # Same folder + a sub/note.md
        with_sub = _write_runbook_folder(tmp_path, "with-sub")
        (with_sub / "sub").mkdir()
        (with_sub / "sub" / "note.md").write_text("# helper note\n")
        with_sub_hash = compute_runbook_content_hash(with_sub)

        assert base_hash != with_sub_hash
        assert with_sub_hash.startswith(HASH_PREFIX)

    def test_rejects_non_ascii_filename(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        (folder / "réadme.md").write_text("x", encoding="utf-8")
        with pytest.raises(RunbookHashError):
            compute_runbook_content_hash(folder)

    def test_rejects_oversized_file(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        (folder / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        with pytest.raises(RunbookHashError):
            compute_runbook_content_hash(folder)

    def test_rejects_too_many_files(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        for i in range(49):  # 2 default files + 49 = 51 total
            (folder / f"f{i}.txt").write_text("x", encoding="utf-8")
        with pytest.raises(RunbookHashError):
            compute_runbook_content_hash(folder)

    def test_rejects_non_allowlisted_extension(self, tmp_path: Path) -> None:
        folder = _write_runbook_folder(tmp_path, "rb")
        (folder / "foo.exe").write_bytes(b"x")
        with pytest.raises(RunbookHashError):
            compute_runbook_content_hash(folder)

    def test_baseline_folder_stable_hash(self, tmp_path: Path) -> None:
        """Regression fence: freezes the algorithm for the default fixture folder
        (runbook.yaml + CLAUDE.md only). If this fails after an unrelated change,
        the algorithm drifted — investigate before updating the constant.
        """
        folder = _write_runbook_folder(tmp_path, "rb")
        h = compute_runbook_content_hash(folder)
        # NOTE TO IMPLEMENTER: run this test once locally, capture the printed/actual
        # hash value, and hard-code it below. Do NOT guess the hex digest.
        expected = "v2:sha256:dad62fcc84cc015a3b8b70e33006bbef9d584e55191fa39d0c477ac585c581de"
        assert h == expected

    def test_different_config_different_hash(self, tmp_path: Path) -> None:
        a = _write_runbook_folder(tmp_path / "a", "rb")
        b = _write_runbook_folder(
            tmp_path / "b",
            "rb",
            {
                "runbook.yaml": (
                    (tmp_path / "a" / "rb" / "runbook.yaml")
                    .read_text(encoding="utf-8")
                    .replace("300", "999")
                )
            },
        )
        assert compute_runbook_content_hash(a) != compute_runbook_content_hash(b)
