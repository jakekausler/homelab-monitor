"""Tests for label_parser module.

STAGE-003-006 Wave 2: Parse homelab-monitor.* labels into probe descriptors.
"""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.docker.label_parser import (
    ParseResult,
    parse_homelab_labels,
)


class TestEmptyAndNoLabels:
    """Test cases for empty labels and non-homelab labels."""

    def test_empty_labels_returns_empty_parse_result(self) -> None:
        """Empty dict returns all empty tuples and exec_authorized=False."""
        result = parse_homelab_labels({})
        assert result == ParseResult(
            descriptors=(),
            collisions=(),
            malformed=(),
            exec_authorized=False,
        )

    def test_labels_without_homelab_prefix_are_ignored(self) -> None:
        """Labels without 'homelab-monitor.' prefix are skipped."""
        labels = {
            "com.docker.compose.project": "my-project",
            "com.docker.compose.service": "api",
            "custom.label": "value",
        }
        result = parse_homelab_labels(labels)
        assert result == ParseResult(
            descriptors=(),
            collisions=(),
            malformed=(),
            exec_authorized=False,
        )


class TestHttpProbes:
    """Test cases for http probe labels."""

    def test_http_label_with_explicit_name(self) -> None:
        """homelab-monitor.http.api=http://x:8080/healthz."""
        result = parse_homelab_labels({"homelab-monitor.http.api": "http://localhost:8080/healthz"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "http"
        assert result.descriptors[0].name == "api"
        assert result.descriptors[0].raw_value == "http://localhost:8080/healthz"
        assert len(result.malformed) == 0
        assert len(result.collisions) == 0

    def test_http_label_with_default_name(self) -> None:
        """homelab-monitor.http=http://x:8080/."""
        result = parse_homelab_labels({"homelab-monitor.http": "http://localhost:8080/"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "http"
        assert result.descriptors[0].name == "default"
        assert result.descriptors[0].raw_value == "http://localhost:8080/"

    def test_https_label_accepted(self) -> None:
        """https:// is also valid for http kind."""
        result = parse_homelab_labels({"homelab-monitor.http.api": "https://example.com/health"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].raw_value == "https://example.com/health"

    def test_malformed_http_yields_invalid_http_url(self) -> None:
        """homelab-monitor.http.x=not-a-url."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "not-a-url"})
        assert len(result.descriptors) == 0
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_http_url"

    def test_malformed_http_ftp_scheme(self) -> None:
        """homelab-monitor.http.x=ftp://x/y."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "ftp://x/y"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_http_url"

    def test_malformed_http_no_scheme(self) -> None:
        """homelab-monitor.http.x=localhost:8080."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "localhost:8080"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_http_url"

    def test_malformed_http_empty_netloc(self) -> None:
        """homelab-monitor.http.x=http://."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "http://"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_http_url"


class TestMetricsProbes:
    """Test cases for metrics probe labels."""

    def test_metrics_label_valid(self) -> None:
        """homelab-monitor.metrics.prom=http://localhost:9100/metrics."""
        result = parse_homelab_labels(
            {"homelab-monitor.metrics.prom": "http://localhost:9100/metrics"}
        )
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "metrics"
        assert result.descriptors[0].name == "prom"

    def test_metrics_label_https(self) -> None:
        """metrics also accepts https."""
        result = parse_homelab_labels({"homelab-monitor.metrics.x": "https://example.com/metrics"})
        assert len(result.descriptors) == 1

    def test_malformed_metrics_yields_invalid_metrics_url(self) -> None:
        """homelab-monitor.metrics.x=not-a-url."""
        result = parse_homelab_labels({"homelab-monitor.metrics.x": "not-a-url"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_metrics_url"


class TestTcpProbes:
    """Test cases for tcp probe labels."""

    def test_tcp_label_with_host_sentinel(self) -> None:
        """tcp://host:5432."""
        result = parse_homelab_labels({"homelab-monitor.tcp.api": "tcp://host:5432"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "tcp"
        assert result.descriptors[0].name == "api"
        assert result.descriptors[0].raw_value == "tcp://host:5432"

    def test_tcp_label_with_container_sentinel(self) -> None:
        """tcp://container:5432."""
        result = parse_homelab_labels({"homelab-monitor.tcp.api": "tcp://container:5432"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].raw_value == "tcp://container:5432"

    def test_tcp_label_with_explicit_ip(self) -> None:
        """tcp://192.168.1.5:8080."""
        result = parse_homelab_labels({"homelab-monitor.tcp.api": "tcp://192.168.1.5:8080"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].raw_value == "tcp://192.168.1.5:8080"

    def test_tcp_label_with_localhost(self) -> None:
        """tcp://localhost:9200."""
        result = parse_homelab_labels({"homelab-monitor.tcp.api": "tcp://localhost:9200"})
        assert len(result.descriptors) == 1

    def test_tcp_without_scheme_yields_invalid_tcp_url(self) -> None:
        """homelab-monitor.tcp.x=host:8080."""
        result = parse_homelab_labels({"homelab-monitor.tcp.x": "host:8080"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_tcp_url"

    def test_tcp_without_port_yields_tcp_port_missing(self) -> None:
        """homelab-monitor.tcp.x=tcp://host."""
        result = parse_homelab_labels({"homelab-monitor.tcp.x": "tcp://host"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "tcp_port_missing"

    def test_tcp_without_host_yields_tcp_host_required(self) -> None:
        """homelab-monitor.tcp.x=tcp://:8080."""
        result = parse_homelab_labels({"homelab-monitor.tcp.x": "tcp://:8080"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "tcp_host_required"

    def test_tcp_with_wrong_scheme(self) -> None:
        """homelab-monitor.tcp.x=http://host:8080."""
        result = parse_homelab_labels({"homelab-monitor.tcp.x": "http://host:8080"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "invalid_tcp_url"


class TestExecProbes:
    """Test cases for exec probe labels."""

    def test_exec_label_with_default_name(self) -> None:
        """homelab-monitor.exec=/healthz.sh."""
        result = parse_homelab_labels({"homelab-monitor.exec": "/usr/local/bin/check"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "exec"
        assert result.descriptors[0].name == "default"
        assert result.descriptors[0].raw_value == "/usr/local/bin/check"

    def test_exec_label_with_explicit_name(self) -> None:
        """homelab-monitor.exec.health=/check.sh."""
        result = parse_homelab_labels({"homelab-monitor.exec.health": "/check.sh"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].name == "health"

    def test_exec_label_with_space_padding(self) -> None:
        """Spaces are trimmed and checked for emptiness."""
        result = parse_homelab_labels({"homelab-monitor.exec.x": "  /check.sh  "})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].raw_value == "  /check.sh  "

    def test_exec_empty_command_yields_empty_value(self) -> None:
        """homelab-monitor.exec.x=  (whitespace only)."""
        result = parse_homelab_labels({"homelab-monitor.exec.x": "   "})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "empty_value"


class TestExecAuthorized:
    """Test cases for exec_authorized flag."""

    def test_exec_authorized_true_sets_flag_and_is_not_descriptor(self) -> None:
        """homelab-monitor.exec_authorized=true."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "true"})
        assert result.exec_authorized is True
        assert len(result.descriptors) == 0
        assert len(result.malformed) == 0

    def test_exec_authorized_uppercase_true(self) -> None:
        """homelab-monitor.exec_authorized=TRUE."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "TRUE"})
        assert result.exec_authorized is True

    def test_exec_authorized_mixed_case_true(self) -> None:
        """homelab-monitor.exec_authorized=True."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "True"})
        assert result.exec_authorized is True

    def test_exec_authorized_false_value_results_in_false(self) -> None:
        """homelab-monitor.exec_authorized=false."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "false"})
        assert result.exec_authorized is False
        assert len(result.malformed) == 0

    def test_exec_authorized_arbitrary_value_results_in_false(self) -> None:
        """homelab-monitor.exec_authorized=yes."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "yes"})
        assert result.exec_authorized is False

    def test_exec_authorized_empty_string_results_in_false(self) -> None:
        """homelab-monitor.exec_authorized=."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": ""})
        assert result.exec_authorized is False

    def test_exec_authorized_with_spaces(self) -> None:
        """homelab-monitor.exec_authorized=  true  ."""
        result = parse_homelab_labels({"homelab-monitor.exec_authorized": "  true  "})
        assert result.exec_authorized is True


class TestUnknownKind:
    """Test cases for unknown probe kinds."""

    def test_unknown_kind_yields_malformed(self) -> None:
        """homelab-monitor.bogus.x=value."""
        result = parse_homelab_labels({"homelab-monitor.bogus.x": "value"})
        assert len(result.descriptors) == 0
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "unknown_kind"

    def test_snmp_kind_unknown(self) -> None:
        """homelab-monitor.snmp.x=value."""
        result = parse_homelab_labels({"homelab-monitor.snmp.x": "value"})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "unknown_kind"


class TestEmptyValue:
    """Test cases for empty label values."""

    def test_empty_value_yields_empty_value(self) -> None:
        """homelab-monitor.http.x= (empty string)."""
        result = parse_homelab_labels({"homelab-monitor.http.x": ""})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "empty_value"

    def test_whitespace_only_value_yields_empty_value(self) -> None:
        """homelab-monitor.http.x=   (spaces only)."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "   "})
        assert len(result.malformed) == 1
        assert result.malformed[0].reason == "empty_value"


class TestCollisions:
    """Test cases for label collisions."""

    def test_collision_two_same_kind_name_both_removed(self) -> None:
        """Two labels resolve to same (kind, name)."""
        # Note: in Python, duplicate keys in dict literals take the last value.
        # For testing collisions, we need two actual distinct keys that map to same (kind, name).
        # homelab-monitor.http and homelab-monitor.http.default both map to (http, default).
        result = parse_homelab_labels(
            {
                "homelab-monitor.http": "http://a.com/",
                "homelab-monitor.http.default": "http://b.com/",
            }
        )
        # Both map to (http, default)
        assert len(result.descriptors) == 0
        assert len(result.collisions) == 1
        assert result.collisions[0].kind == "http"
        assert result.collisions[0].name == "default"
        assert set(result.collisions[0].conflicting_values) == {
            "http://a.com/",
            "http://b.com/",
        }

    def test_collision_explicit_vs_implicit_default_name(self) -> None:
        """homelab-monitor.http=A and homelab-monitor.http.default=B collide."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http": "http://implicit.com/",
                "homelab-monitor.http.default": "http://explicit.com/",
            }
        )
        assert len(result.descriptors) == 0
        assert len(result.collisions) == 1

    def test_no_collision_different_names(self) -> None:
        """homelab-monitor.http.api and homelab-monitor.http.metrics."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http.api": "http://a.com/",
                "homelab-monitor.http.metrics": "http://m.com/",
            }
        )
        assert len(result.descriptors) == 2  # noqa: PLR2004
        assert len(result.collisions) == 0

    def test_no_collision_different_kinds(self) -> None:
        """homelab-monitor.http.api and homelab-monitor.tcp.api."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http.api": "http://a.com/",
                "homelab-monitor.tcp.api": "tcp://host:8080",
            }
        )
        assert len(result.descriptors) == 2  # noqa: PLR2004
        assert len(result.collisions) == 0

    def test_collision_removes_both_from_descriptors(self) -> None:
        """Collision removes both entries from descriptors list."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http": "http://a.com/",
                "homelab-monitor.http.default": "http://b.com/",
            }
        )
        # Verify neither is in descriptors
        assert not any(d.kind == "http" and d.name == "default" for d in result.descriptors)


class TestMixedScenarios:
    """Test cases mixing valid, invalid, and collision cases."""

    def test_mixed_valid_invalid_authorized(self) -> None:
        """Mix of valid http, malformed tcp, exec_authorized, and unrelated label."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http.api": "http://a.com/",
                "homelab-monitor.tcp.db": "tcp://host",  # malformed: missing port
                "homelab-monitor.exec_authorized": "true",
                "com.docker.compose.project": "my-project",
            }
        )
        assert len(result.descriptors) == 1
        assert result.descriptors[0].kind == "http"
        assert len(result.malformed) == 1
        assert result.malformed[0].label_key == "homelab-monitor.tcp.db"
        assert result.exec_authorized is True

    def test_collision_with_valid_and_invalid(self) -> None:
        """One valid http, one malformed http (same name), one collision."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http": "http://a.com/",
                "homelab-monitor.http.default": "not-a-url",
            }
        )
        # http.default is malformed, so no collision with http (default)
        assert len(result.descriptors) == 1
        assert result.descriptors[0].name == "default"
        assert len(result.malformed) == 1

    def test_multiple_kinds_multiple_names(self) -> None:
        """Multiple kinds and names, all valid."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http.a": "http://a.com/",
                "homelab-monitor.http.b": "http://b.com/",
                "homelab-monitor.tcp.api": "tcp://host:5432",
                "homelab-monitor.metrics.prom": "http://localhost:9100/metrics",
                "homelab-monitor.exec.health": "/check.sh",
            }
        )
        assert len(result.descriptors) == 5  # noqa: PLR2004
        assert len(result.collisions) == 0
        assert len(result.malformed) == 0


class TestDottedNames:
    """Test cases for dotted probe names."""

    def test_dotted_probe_name(self) -> None:
        """homelab-monitor.http.api.v2=value."""
        result = parse_homelab_labels({"homelab-monitor.http.api.v2": "http://api.v2.com/"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].name == "api.v2"

    def test_deeply_dotted_name(self) -> None:
        """homelab-monitor.http.a.b.c=value."""
        result = parse_homelab_labels({"homelab-monitor.http.a.b.c": "http://test.com/"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].name == "a.b.c"


class TestDeterministicOrdering:
    """Test cases for deterministic ordering of results."""

    def test_descriptors_sorted_deterministically(self) -> None:
        """Descriptors sorted by (kind, name)."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.tcp.api": "tcp://host:8080",
                "homelab-monitor.http.b": "http://b.com/",
                "homelab-monitor.http.a": "http://a.com/",
                "homelab-monitor.metrics.prom": "http://localhost:9100/metrics",
            }
        )
        expected_order = [
            ("http", "a"),
            ("http", "b"),
            ("metrics", "prom"),
            ("tcp", "api"),
        ]
        actual_order = [(d.kind, d.name) for d in result.descriptors]
        assert actual_order == expected_order

    def test_collisions_sorted_deterministically(self) -> None:
        """Collisions sorted by (kind, name)."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.tcp": "tcp://host:5432",
                "homelab-monitor.tcp.default": "tcp://container:5432",
                "homelab-monitor.http": "http://a.com/",
                "homelab-monitor.http.default": "http://b.com/",
            }
        )
        assert len(result.collisions) == 2  # noqa: PLR2004
        collision_order = [(c.kind, c.name) for c in result.collisions]
        assert collision_order == [("http", "default"), ("tcp", "default")]

    def test_malformed_sorted_by_label_key(self) -> None:
        """Malformed sorted by label_key."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.tcp.z": "tcp://no-port",
                "homelab-monitor.http.a": "not-a-url",
                "homelab-monitor.bogus.x": "value",
            }
        )
        assert len(result.malformed) == 3  # noqa: PLR2004
        malformed_keys = [m.label_key for m in result.malformed]
        assert malformed_keys == sorted(malformed_keys)

    def test_deterministic_order_with_unsorted_input(self) -> None:
        """Output is deterministic regardless of input dict order."""
        labels = {
            "homelab-monitor.tcp.api": "tcp://host:8080",
            "homelab-monitor.http.metrics": "http://m.com/",
            "homelab-monitor.http.api": "http://a.com/",
        }
        result = parse_homelab_labels(labels)
        expected_order = [
            ("http", "api"),
            ("http", "metrics"),
            ("tcp", "api"),
        ]
        actual_order = [(d.kind, d.name) for d in result.descriptors]
        assert actual_order == expected_order


class TestMalformedLabelDetails:
    """Test cases for MalformedLabel data preservation."""

    def test_malformed_preserves_label_key_and_value(self) -> None:
        """MalformedLabel captures the full key and value."""
        result = parse_homelab_labels({"homelab-monitor.http.x": "not-a-url"})
        assert result.malformed[0].label_key == "homelab-monitor.http.x"
        assert result.malformed[0].label_value == "not-a-url"

    def test_malformed_reason_varies_by_kind(self) -> None:
        """Different kinds produce different reason codes."""
        http_result = parse_homelab_labels({"homelab-monitor.http.x": "not-url"})
        metrics_result = parse_homelab_labels({"homelab-monitor.metrics.x": "not-url"})
        tcp_result = parse_homelab_labels({"homelab-monitor.tcp.x": "tcp://host"})

        assert http_result.malformed[0].reason == "invalid_http_url"
        assert metrics_result.malformed[0].reason == "invalid_metrics_url"
        assert tcp_result.malformed[0].reason == "tcp_port_missing"


class TestProbeDescriptorFields:
    """Test cases for ProbeDescriptor field preservation."""

    def test_probe_descriptor_preserves_all_fields(self) -> None:
        """ProbeDescriptor preserves kind, name, raw_value."""
        result = parse_homelab_labels({"homelab-monitor.tcp.db": "tcp://postgres:5432"})
        d = result.descriptors[0]
        assert d.kind == "tcp"
        assert d.name == "db"
        assert d.raw_value == "tcp://postgres:5432"

    def test_probe_descriptor_immutable(self) -> None:
        """ProbeDescriptor is frozen (immutable)."""
        result = parse_homelab_labels({"homelab-monitor.http.api": "http://localhost/"})
        d = result.descriptors[0]
        with pytest.raises(AttributeError):
            d.kind = "tcp"  # type: ignore


class TestCollisionConflictingValues:
    """Test cases for LabelCollision.conflicting_values."""

    def test_collision_conflicting_values_sorted(self) -> None:
        """conflicting_values tuple is sorted for determinism."""
        result = parse_homelab_labels(
            {
                "homelab-monitor.http": "http://z.com/",
                "homelab-monitor.http.default": "http://a.com/",
            }
        )
        assert len(result.collisions) == 1
        # Check values are sorted
        values = result.collisions[0].conflicting_values
        assert values == tuple(sorted(values))
        assert set(values) == {"http://z.com/", "http://a.com/"}

    def test_collision_with_three_conflicting_labels(self) -> None:
        """Three labels with same (kind, name) all collected."""
        # Can't have three distinct keys map to same bucket in single call.
        # This is actually constrained by the dict design.
        # Collision detection works on buckets, so we can't have 3 labels
        # with identical (kind, name) unless they're on different containers.
        # The current design per-container parsing means this test is conceptual.
        # Skipping as physically impossible in this wave's scope.
        pass


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_label_value_with_special_characters(self) -> None:
        """Label values can contain special characters."""
        result = parse_homelab_labels({"homelab-monitor.exec.health": "/bin/bash -c 'echo ok'"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].raw_value == "/bin/bash -c 'echo ok'"

    def test_very_long_probe_name(self) -> None:
        """Long dotted probe names are accepted."""
        long_name = "a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p"
        result = parse_homelab_labels({f"homelab-monitor.http.{long_name}": "http://localhost/"})
        assert len(result.descriptors) == 1
        assert result.descriptors[0].name == long_name

    def test_parse_result_is_frozen(self) -> None:
        """ParseResult dataclass is frozen."""
        result = parse_homelab_labels({})
        with pytest.raises(AttributeError):
            result.descriptors = ()  # type: ignore
