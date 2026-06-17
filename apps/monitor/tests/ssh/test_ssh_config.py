"""Tests for SSH targets config loading (STAGE-017-002)."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.ssh import (
    SshTargetParams,
    load_ssh_target_configs,
    load_ssh_targets,
)
from homelab_monitor.kernel.ssh.config import SshTargetConfig

_DEFAULT_SSH_PORT = 22
_SYNOLOGY_SSH_PORT = 53197


def _write_config(tmp_path: Path, body: str) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(body)
    return config_file


class TestLoadSshTargets:
    """Test SSH targets loading from YAML config."""

    def test_absent_file_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Missing config file → {}."""
        config_path = tmp_path / "nonexistent.yaml"
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_path))
        result = load_ssh_targets()
        assert result == {}

    def test_section_absent_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Config body without ssh_targets key → {}."""
        config_file = _write_config(tmp_path, "other_key: 1\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert result == {}

    def test_section_null_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets: (null) → {}."""
        config_file = _write_config(tmp_path, "ssh_targets:\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert result == {}

    def test_section_empty_list_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets: [] → {}."""
        config_file = _write_config(tmp_path, "ssh_targets: []\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert result == {}

    def test_valid_appliance_entry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Valid appliance entry with defaults."""
        body = """ssh_targets:
  - id: "udm"
    host: "192.168.2.1"
    user: "root"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert len(result) == 1
        assert "udm" in result
        params = result["udm"]
        assert isinstance(params, SshTargetParams)
        assert params.host == "192.168.2.1"
        assert params.port == _DEFAULT_SSH_PORT
        assert params.user == "root"
        assert params.key_secret_name == "ssh_probe_key_udm"
        assert params.pinned_host_key is None
        assert params.account_mode == "appliance"

    def test_empty_key_secret_ref_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Empty key_secret_ref defaults to ssh_probe_key_<id>."""
        body = """ssh_targets:
  - id: "udm"
    host: "192.168.2.1"
    user: "root"
    account_mode: "appliance"
    key_secret_ref: ""
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert len(result) == 1
        params = result["udm"]
        assert params.key_secret_name == "ssh_probe_key_udm"

    def test_valid_dedicated_user_entry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Valid dedicated-user entry with custom port/user/secret-ref."""
        body = """ssh_targets:
  - id: "synology"
    host: "192.168.2.4"
    port: 53197
    user: "monitor"
    account_mode: "dedicated-user"
    key_secret_ref: "custom_key_name"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        params = result["synology"]
        assert params.account_mode == "dedicated_user"
        assert params.port == _SYNOLOGY_SSH_PORT
        assert params.user == "monitor"
        assert params.key_secret_name == "custom_key_name"
        assert params.pinned_host_key is None

    def test_host_key_valid_ed25519_stored_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Valid ed25519 host key stored verbatim including comment."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    host_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc123def456 comment@host"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        params = result["t"]
        assert (
            params.pinned_host_key
            == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc123def456 comment@host"
        )

    def test_host_key_valid_rsa_stored_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Valid RSA host key stored verbatim."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    host_key: "ssh-rsa AAAAB3NzaC1yc2EAAAAFOOBAR"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        params = result["t"]
        assert params.pinned_host_key == "ssh-rsa AAAAB3NzaC1yc2EAAAAFOOBAR"

    def test_host_key_keyscan_line_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Host key with leading hostname rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    host_key: "192.168.2.1 ssh-ed25519 AAAAC3Nz"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is not a known SSH key-type"):
            load_ssh_targets()

    def test_host_key_known_hosts_hashed_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Host key with |1| hashed token rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    host_key: "|1|abc=|def= ssh-ed25519 AAAA"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is not a known SSH key-type"):
            load_ssh_targets()

    def test_host_key_too_few_tokens_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Host key with single token rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    host_key: "ssh-ed25519"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="too few whitespace-separated tokens"):
            load_ssh_targets()

    def test_account_mode_invalid_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Invalid account_mode value rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "superuser"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_targets()

    def test_missing_required_host_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entry missing host field rejected."""
        body = """ssh_targets:
  - id: "t"
    user: "u"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_targets()

    def test_extra_field_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Entry with unknown field rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    bogus: "x"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_targets()

    def test_forced_command_and_script_id_both_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entry with both forced_command and script_id rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    forced_command: "run-x"
    script_id: "s1"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="at most one of"):
            load_ssh_targets()

    def test_forced_command_alone_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entry with only forced_command accepted."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    forced_command: "run-x"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert len(result) == 1
        assert result["t"].host == "h"

    def test_script_id_alone_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Entry with only script_id accepted."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    script_id: "s1"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert len(result) == 1
        assert result["t"].host == "h"

    def test_duplicate_id_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Duplicate id values rejected."""
        body = """ssh_targets:
  - id: "dup"
    host: "h1"
    user: "u"
    account_mode: "appliance"
  - id: "dup"
    host: "h2"
    user: "u"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="duplicate id"):
            load_ssh_targets()

    def test_port_zero_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Port 0 rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    port: 0
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_targets()

    def test_port_too_large_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Port 70000 rejected."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    port: 70000
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_targets()

    def test_root_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Config root as list rejected."""
        body = "- just\n- a\n- list\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="config root must be a mapping"):
            load_ssh_targets()

    def test_section_not_list_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets as mapping rejected."""
        body = "ssh_targets:\n  a: 1\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="ssh_targets must be a list"):
            load_ssh_targets()

    def test_entry_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """List entry as string rejected."""
        body = "ssh_targets:\n  - just-a-string\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match=r"ssh_targets\[0\] must be a mapping"):
            load_ssh_targets()

    def test_resolver_dict_semantics(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Resolver supports dict.get semantics."""
        body = """ssh_targets:
  - id: "udm"
    host: "192.168.2.1"
    user: "root"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert result.get("udm") is not None
        assert isinstance(result.get("udm"), SshTargetParams)
        assert result.get("unknown") is None

    def test_concurrency_group_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """concurrency_group field accepted."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    concurrency_group: "appliances"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_targets()
        assert len(result) == 1
        assert result["t"].host == "h"


class TestLoadSshTargetConfigs:
    """Test the un-projected SSH targets config loader."""

    def test_returns_unprojected_config_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """load_ssh_target_configs returns SshTargetConfig with preserved fields."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "appliance"
    forced_command: "run-x"
    concurrency_group: "appliances"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        assert len(result) == 1
        cfg = result["t"]
        assert isinstance(cfg, SshTargetConfig)
        assert cfg.forced_command == "run-x"
        assert cfg.account_mode == "appliance"
        assert cfg.concurrency_group == "appliances"
        assert cfg.script_id is None

    def test_script_id_preserved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """load_ssh_target_configs preserves script_id."""
        body = """ssh_targets:
  - id: "t"
    host: "h"
    user: "u"
    account_mode: "dedicated-user"
    script_id: "s1"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        cfg = result["t"]
        assert cfg.script_id == "s1"
        assert cfg.account_mode == "dedicated-user"

    def test_absent_file_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Missing config file → {}."""
        config_path = tmp_path / "nonexistent.yaml"
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_path))
        result = load_ssh_target_configs()
        assert result == {}

    def test_section_null_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets: (null) → {}."""
        config_file = _write_config(tmp_path, "ssh_targets:\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        assert result == {}

    def test_section_empty_list_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets: [] → {}."""
        config_file = _write_config(tmp_path, "ssh_targets: []\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        assert result == {}

    def test_section_absent_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Config body without ssh_targets key → {}."""
        config_file = _write_config(tmp_path, "other_key: 1\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        assert result == {}

    def test_duplicate_id_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Duplicate id values rejected."""
        body = """ssh_targets:
  - id: "dup"
    host: "h1"
    user: "u"
    account_mode: "appliance"
  - id: "dup"
    host: "h2"
    user: "u"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="duplicate id"):
            load_ssh_target_configs()

    def test_bad_entry_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Entry missing required host field rejected."""
        body = """ssh_targets:
  - id: "t"
    user: "u"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="is invalid"):
            load_ssh_target_configs()

    def test_root_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Config root as list rejected."""
        body = "- just\n- a\n- list\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="config root must be a mapping"):
            load_ssh_target_configs()

    def test_section_not_list_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ssh_targets as mapping rejected."""
        body = "ssh_targets:\n  a: 1\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="ssh_targets must be a list"):
            load_ssh_target_configs()

    def test_entry_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """List entry as string rejected."""
        body = "ssh_targets:\n  - just-a-string\n"
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match=r"ssh_targets\[0\] must be a mapping"):
            load_ssh_target_configs()

    def test_key_secret_ref_default_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """key_secret_ref default applies when omitted."""
        body = """ssh_targets:
  - id: "udm"
    host: "h"
    user: "u"
    account_mode: "appliance"
"""
        config_file = _write_config(tmp_path, body)
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_ssh_target_configs()
        assert result["udm"].key_secret_ref == "ssh_probe_key_udm"
