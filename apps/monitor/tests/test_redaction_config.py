"""Tests for log redaction configuration loading (STAGE-004-006)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homelab_monitor.kernel.config import (
    DEFAULT_REDACT_PATTERNS,
    load_redact_patterns,
)


class TestLoadRedactPatterns:
    """Test redaction pattern loading from YAML config."""

    def test_absent_file_returns_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Missing config file → DEFAULT_REDACT_PATTERNS."""
        config_path = tmp_path / "nonexistent.yaml"
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_path))
        result = load_redact_patterns()
        assert len(result) == 5  # noqa: PLR2004
        assert result == list(DEFAULT_REDACT_PATTERNS)

    def test_absent_logs_section_returns_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """YAML with only disk_budget: → defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("disk_budget:\n  total_gb: 100\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_redact_patterns()
        assert len(result) == 5  # noqa: PLR2004
        assert result == list(DEFAULT_REDACT_PATTERNS)

    def test_absent_redact_subkey_returns_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """logs: without redact: → defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("logs:\n  something_else: 1\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_redact_patterns()
        assert len(result) == 5  # noqa: PLR2004

    def test_redact_none_returns_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """logs: redact: (None) → defaults."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("logs:\n  redact:\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_redact_patterns()
        assert len(result) == 5  # noqa: PLR2004

    def test_explicit_empty_list_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """logs: redact: [] (explicit empty) → []."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("logs:\n  redact: []\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_redact_patterns()
        assert result == []

    def test_valid_single_pattern(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """One well-formed entry → list len 1."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            '    - name: "test_pattern"\n'
            '      pattern: "secret=.+"\n'
            '      replacement: "secret=[REDACTED]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        result = load_redact_patterns()
        assert len(result) == 1
        assert result[0].name == "test_pattern"
        assert result[0].pattern == "secret=.+"
        assert result[0].replacement == "secret=[REDACTED]"

    def test_missing_field_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Entry missing replacement → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text('logs:\n  redact:\n    - name: "test"\n      pattern: "secret=.+"\n')
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="must be a non-empty string"):
            load_redact_patterns()

    def test_empty_string_field_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """name: "" → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            '    - name: ""\n'
            '      pattern: "secret=.+"\n'
            '      replacement: "secret=[REDACTED]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="must be a non-empty string"):
            load_redact_patterns()

    def test_non_string_field_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pattern: 42 → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            "    - name: test\n"
            "      pattern: 42\n"
            '      replacement: "secret=[REDACTED]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="must be a non-empty string"):
            load_redact_patterns()

    def test_duplicate_name_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Two entries same name → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            "    - name: dup\n"
            '      pattern: "secret1"\n'
            '      replacement: "[R1]"\n'
            "    - name: dup\n"
            '      pattern: "secret2"\n'
            '      replacement: "[R2]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="duplicate name"):
            load_redact_patterns()

    def test_bad_name_format_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """name: "Bearer Token" → ValueError (not snake_case)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            '    - name: "Bearer Token"\n'
            '      pattern: "bearer"\n'
            '      replacement: "[R]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="snake_case"):
            load_redact_patterns()

    @pytest.mark.parametrize(
        "lookaround",
        [
            "(?=foo)bar",
            "(?!foo)bar",
            "(?<=foo)bar",
            "(?<!foo)bar",
        ],
    )
    def test_lookaround_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lookaround: str
    ) -> None:
        """pattern: with lookaround → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "logs:\n"
            "  redact:\n"
            '    - name: "test_pattern"\n'
            f'      pattern: "{lookaround}"\n'
            '      replacement: "[R]"\n'
        )
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="lookaround"):
            load_redact_patterns()

    def test_redact_not_list_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """logs: redact: 42 → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("logs:\n  redact: 42\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="must be a list"):
            load_redact_patterns()

    def test_logs_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """logs: 42 → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("logs: 42\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="logs must be a mapping"):
            load_redact_patterns()

    def test_entry_not_dict_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """logs.redact entry that is a string (not a mapping) → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text('logs:\n  redact:\n    - "not_a_dict"\n')
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="must be a mapping"):
            load_redact_patterns()

    def test_root_not_mapping_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """root: [a] (list) → ValueError."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- a\n")
        monkeypatch.setenv("HOMELAB_MONITOR_CONFIG", str(config_file))
        with pytest.raises(ValueError, match="config root must be a mapping"):
            load_redact_patterns()

    def test_defaults_content(self) -> None:
        """Assert 5 defaults with exact names and no lookarounds."""
        assert len(DEFAULT_REDACT_PATTERNS) == 5  # noqa: PLR2004
        names = [p.name for p in DEFAULT_REDACT_PATTERNS]
        assert names == [
            "bearer_token",
            "jwt",
            "password_in_url",
            "aws_access_key",
            "api_key_generic",
        ]
        for pattern in DEFAULT_REDACT_PATTERNS:
            for token in ("(?=", "(?!", "(?<=", "(?<!"):
                assert token not in pattern.pattern

    def test_default_patterns_compile_with_python_re(self) -> None:
        """Each default pattern compiles with re.compile (smoke test)."""
        for pattern in DEFAULT_REDACT_PATTERNS:
            try:
                re.compile(pattern.pattern)
            except Exception as exc:
                pytest.fail(f"Pattern {pattern.name} failed to compile: {exc}")
