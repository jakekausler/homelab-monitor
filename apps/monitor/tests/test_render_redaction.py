"""Tests for redaction VRL rendering (STAGE-004-006)."""

from __future__ import annotations

from homelab_monitor.kernel.config import DEFAULT_REDACT_PATTERNS, RedactPattern
from homelab_monitor.kernel.cron.render import (
    build_redact_metric_entries,
    build_redact_strip_markers,
    build_redact_vrl,
)


class TestBuildRedactVrl:
    """Test VRL generation for redaction remap transforms."""

    def test_empty_returns_noop(self) -> None:
        """Empty pattern list → no-op comment."""
        result = build_redact_vrl([])
        assert result == "# no redaction patterns configured\n"

    def test_one_pattern_structure(self) -> None:
        """One pattern → if/match/replace/marker structure."""
        pattern = RedactPattern(
            name="test",
            pattern="secret=.+",
            replacement="secret=[REDACTED]",
        )
        result = build_redact_vrl([pattern])
        assert "if match(to_string(.message) ?? \"\", r'secret=.+')" in result
        assert 'replace(to_string(.message) ?? "", r\'secret=.+\', "secret=[REDACTED]")' in result
        assert ".rdt_test = 0" in result
        assert ".rdt_test = 1" in result

    def test_password_in_url_slashes_unescaped(self) -> None:
        """password_in_url default → slashes NOT escaped in raw-string regex form."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        assert r"://[^:@/\s]+:[^@/\s]+@" in result
        assert r":\/\/" not in result

    def test_all_defaults_present(self) -> None:
        """All 8 defaults present in output."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        assert ".rdt_bearer_token = 1" in result
        assert ".rdt_jwt = 1" in result
        assert ".rdt_password_in_url = 1" in result
        assert ".rdt_aws_access_key = 1" in result
        assert ".rdt_api_key_generic = 1" in result
        assert ".rdt_udm_bearer = 1" in result
        assert ".rdt_udm_session = 1" in result
        assert ".rdt_udm_authkey = 1" in result

    def test_no_lookarounds_in_output(self) -> None:
        """Generated output contains no lookaround tokens."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        for token in ("(?=", "(?!", "(?<=", "(?<!"):
            assert token not in result

    def test_inline_flags_retained_in_raw_string(self) -> None:
        """(?i) inline flag groups are retained inside r'...' raw-string regexes."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        assert "r'(?i)bearer" in result
        assert "(?i)(api[-_]?key" in result
        jwt_lines = [ln for ln in result.splitlines() if "eyJ" in ln and "match" in ln]
        assert jwt_lines and "(?i)" not in jwt_lines[0]

    def test_api_key_generic_replacement_escapes_dollar(self) -> None:
        """api_key_generic replacement renders with $$ so Vector env interpolation
        emits a literal ${1} backref for VRL replace()."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        assert "$${1}=[REDACTED]" in result

    def test_api_key_generic_quote_encoded_as_hex(self) -> None:
        """The literal single-quote in api_key_generic must be \\x27 (raw strings
        have no escape char; a literal ' would terminate r'...')."""
        result = build_redact_vrl(list(DEFAULT_REDACT_PATTERNS))
        assert r"\x27" in result


class TestBuildRedactStripMarkers:
    """Test marker deletion VRL generation."""

    def test_empty_returns_noop(self) -> None:
        """Empty pattern list → no-op comment."""
        result = build_redact_strip_markers([])
        assert result == "# no redaction markers to strip\n"

    def test_all_defaults(self) -> None:
        """All 8 defaults present."""
        result = build_redact_strip_markers(list(DEFAULT_REDACT_PATTERNS))
        assert "del(.rdt_bearer_token)" in result
        assert "del(.rdt_jwt)" in result
        assert "del(.rdt_password_in_url)" in result
        assert "del(.rdt_aws_access_key)" in result
        assert "del(.rdt_api_key_generic)" in result
        assert "del(.rdt_udm_bearer)" in result
        assert "del(.rdt_udm_session)" in result
        assert "del(.rdt_udm_authkey)" in result


class TestBuildRedactMetricEntries:
    """Test metric entry generation for log_to_metric."""

    def test_empty_returns_noop(self) -> None:
        """Empty pattern list → no-op comment."""
        result = build_redact_metric_entries([])
        assert result == "# no redaction metrics configured"

    def test_one_pattern_structure(self) -> None:
        """One pattern → [[transforms.redaction_metric.metrics]] entry."""
        pattern = RedactPattern(
            name="test",
            pattern="secret",
            replacement="[R]",
        )
        result = build_redact_metric_entries([pattern])
        assert "[[transforms.redaction_metric.metrics]]" in result
        assert 'type = "counter"' in result
        assert 'field = "rdt_test"' in result
        assert 'name = "vector_redactions_total"' in result
        assert 'tags.pattern_type = "test"' in result
        assert "increment_by_value = true" in result
        assert "increment_by_value = false" not in result

    def test_pattern_type_is_static_literal(self) -> None:
        """pattern_type is a static literal, never a VRL expression."""
        result = build_redact_metric_entries(list(DEFAULT_REDACT_PATTERNS))
        # Ensure no VRL expressions in pattern_type values
        lines = result.split("\n")
        for line in lines:
            if "tags.pattern_type = " in line:
                # Should be a literal string, not a VRL expression
                assert '"' in line
                # Extract the value
                parts = line.split("=")
                if len(parts) > 1:
                    value = parts[1].strip()
                    # Should start and end with quotes
                    assert value.startswith('"') and value.endswith('"')

    def test_all_defaults_present(self) -> None:
        """All 8 defaults with correct metric names."""
        result = build_redact_metric_entries(list(DEFAULT_REDACT_PATTERNS))
        metric_entries = result.split("[[transforms.redaction_metric.metrics]]")
        # Should have 8 entries (split gives 9 parts: empty + 8 entries)
        assert len(metric_entries) == 9  # noqa: PLR2004
        # Each should have the correct counter name
        assert result.count('name = "vector_redactions_total"') == 8  # noqa: PLR2004
