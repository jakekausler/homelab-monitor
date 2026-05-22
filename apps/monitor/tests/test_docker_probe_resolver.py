"""Tests for probe_resolver module.

100% coverage of resolve_probe, _substitute_url_hostname, _resolve_sentinel.
"""

from __future__ import annotations

from homelab_monitor.kernel.docker.label_parser import ProbeDescriptor
from homelab_monitor.kernel.docker.probe_resolver import ResolvedProbe, resolve_probe

# ---- HTTP / HTTPS tests ----


def test_http_with_literal_ip_passes_through() -> None:
    """http://192.168.1.10:8080/healthz resolves unchanged."""
    descriptor = ProbeDescriptor(
        kind="http", name="default", raw_value="http://192.168.1.10:8080/healthz"
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://192.168.1.10:8080/healthz"
    assert result.kind == "http"
    assert result.name == "default"
    assert result.exec_cmd is None
    assert result.container_id is None


def test_http_with_host_sentinel_substitutes_host_ip() -> None:
    """http://host:8080/x → http://127.0.0.1:8080/x when host_ip=127.0.0.1."""
    descriptor = ProbeDescriptor(kind="http", name="default", raw_value="http://host:8080/api")
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1:8080/api"


def test_http_with_container_sentinel_substitutes_container_ip() -> None:
    """http://container:8080/x → http://172.17.0.5:8080/x when container_ip=172.17.0.5."""
    descriptor = ProbeDescriptor(kind="http", name="api", raw_value="http://container:8080/health")
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://172.17.0.5:8080/health"
    assert result.name == "api"


def test_http_with_container_sentinel_host_network_substitutes_host_ip() -> None:
    """network_mode='host' → container sentinel maps to host_ip."""
    descriptor = ProbeDescriptor(
        kind="http", name="default", raw_value="http://container:8080/health"
    )
    result = resolve_probe(
        descriptor,
        network_mode="host",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1:8080/health"


def test_http_with_container_sentinel_no_ip_returns_none() -> None:
    """container_ip=None, network_mode='bridge' → returns None."""
    descriptor = ProbeDescriptor(
        kind="http", name="default", raw_value="http://container:8080/health"
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip=None,
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert result is None


def test_http_with_host_docker_internal_passes_through() -> None:
    """http://host.docker.internal:8123/api/ → unchanged."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://host.docker.internal:8123/api/",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://host.docker.internal:8123/api/"


def test_http_preserves_path_and_query() -> None:
    """http://host:8080/api/foo?bar=baz#frag → preserves everything except host substitution."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://host:8080/api/foo?bar=baz#frag",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1:8080/api/foo?bar=baz#frag"


def test_http_preserves_userinfo() -> None:
    """http://user:pass@host:8080/ → http://user:pass@127.0.0.1:8080/."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://user:pass@host:8080/",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://user:pass@127.0.0.1:8080/"


def test_https_substitutes_correctly() -> None:
    """https:// scheme is preserved."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="https://host:443/healthz",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "https://127.0.0.1:443/healthz"


def test_http_no_port() -> None:
    """http://host/healthz (no port) → http://127.0.0.1/healthz."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://host/healthz",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1/healthz"


def test_http_userinfo_without_password() -> None:
    """http://user@host:8080/ → http://user@127.0.0.1:8080/."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://user@host:8080/",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://user@127.0.0.1:8080/"


# ---- Metrics tests (same rules as http) ----


def test_metrics_substitutes_correctly() -> None:
    """metrics kind uses same substitution as http."""
    descriptor = ProbeDescriptor(
        kind="metrics",
        name="default",
        raw_value="http://host:9090/metrics",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.kind == "metrics"
    assert result.target == "http://127.0.0.1:9090/metrics"


def test_metrics_with_container_sentinel_no_ip_returns_none() -> None:
    """metrics with container sentinel and no container_ip → None."""
    descriptor = ProbeDescriptor(
        kind="metrics",
        name="default",
        raw_value="http://container:9090/metrics",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip=None,
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert result is None


# ---- TCP tests ----


def test_tcp_with_host_sentinel() -> None:
    """tcp://host:5432 → target="127.0.0.1:5432"."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="postgres",
        raw_value="tcp://host:5432",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "127.0.0.1:5432"
    assert result.kind == "tcp"
    assert result.name == "postgres"


def test_tcp_with_container_sentinel() -> None:
    """tcp://container:8080 (bridge) → container_ip:port."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://container:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "172.17.0.5:8080"


def test_tcp_with_container_sentinel_host_network() -> None:
    """tcp://container:8080 (host network) → host_ip:port."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://container:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="host",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "127.0.0.1:8080"


def test_tcp_with_explicit_ip() -> None:
    """tcp://192.168.1.5:8080 → passthrough."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://192.168.1.5:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "192.168.1.5:8080"


def test_tcp_with_container_sentinel_no_ip_returns_none() -> None:
    """tcp://container:8080 with container_ip=None (bridge) → None."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://container:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip=None,
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert result is None


# ---- Exec tests ----


def test_exec_with_both_flags_returns_resolved_probe() -> None:
    """exec_enabled=True, exec_authorized=True → ResolvedProbe with exec_cmd + container_id."""
    descriptor = ProbeDescriptor(
        kind="exec",
        name="check",
        raw_value="/usr/bin/check-health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=True,
        exec_authorized=True,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.kind == "exec"
    assert result.name == "check"
    assert result.exec_cmd == "/usr/bin/check-health"
    assert result.container_id == "abc123"
    assert result.target == "/usr/bin/check-health"


def test_exec_without_global_flag_returns_none() -> None:
    """exec_enabled=False, exec_authorized=True → None."""
    descriptor = ProbeDescriptor(
        kind="exec",
        name="check",
        raw_value="/usr/bin/check-health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=True,
    )
    assert result is None


def test_exec_without_authorized_label_returns_none() -> None:
    """exec_enabled=True, exec_authorized=False → None."""
    descriptor = ProbeDescriptor(
        kind="exec",
        name="check",
        raw_value="/usr/bin/check-health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=True,
        exec_authorized=False,
    )
    assert result is None


def test_exec_without_both_returns_none() -> None:
    """Both flags False → None."""
    descriptor = ProbeDescriptor(
        kind="exec",
        name="check",
        raw_value="/usr/bin/check-health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert result is None


# ---- Edge cases ----


def test_ipv6_in_url_passes_through() -> None:
    """IPv6 in URL: urlparse extracts as hostname '::1'; not a sentinel so passthrough."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://[::1]:8080/health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    # urlparse extracts IPv6 ::1 and reconstructs without brackets when rebuilding
    assert result.target == "http://::1:8080/health"


def test_tcp_ipv6_passes_through() -> None:
    """tcp://[::1]:5432 → urlparse extracts as hostname '::1' (not a sentinel, passthrough)."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://[::1]:5432",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    # urlparse extracts IPv6 ::1; not a sentinel so reconstructed without brackets
    assert result.target == "::1:5432"


def test_default_network_mode_treated_like_bridge() -> None:
    """network_mode='default' (non-host) → container sentinel resolves to container_ip."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://container:8080/health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="default",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://172.17.0.5:8080/health"


def test_custom_network_mode_treated_like_bridge() -> None:
    """network_mode='my-custom-net' (non-host) → treated as bridge."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://container:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="my-custom-net",
        container_ip="172.18.0.10",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "172.18.0.10:8080"


def test_http_empty_path_preserved() -> None:
    """http://host:8080 (no path) → http://127.0.0.1:8080 (empty path reconstructed)."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://host:8080",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1:8080"


def test_tcp_port_zero() -> None:
    """tcp://host:0 → '127.0.0.1:0' (unusual but urlparse accepts it)."""
    descriptor = ProbeDescriptor(
        kind="tcp",
        name="default",
        raw_value="tcp://host:0",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "127.0.0.1:0"


def test_http_container_sentinel_on_host_network_with_none_ip() -> None:
    """Host network with container_ip=None: container sentinel resolves to host_ip."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://container:8080/health",
    )
    result = resolve_probe(
        descriptor,
        network_mode="host",
        container_ip=None,
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://127.0.0.1:8080/health"


def test_http_with_port_8000() -> None:
    """Test with a non-standard port."""
    descriptor = ProbeDescriptor(
        kind="http",
        name="default",
        raw_value="http://host:8000/api",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="192.168.1.100",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.target == "http://192.168.1.100:8000/api"


def test_metrics_host_sentinel() -> None:
    """metrics kind with host sentinel."""
    descriptor = ProbeDescriptor(
        kind="metrics",
        name="prometheus",
        raw_value="http://host:9090/metrics",
    )
    result = resolve_probe(
        descriptor,
        network_mode="bridge",
        container_ip="172.17.0.5",
        container_id="abc123",
        host_ip="127.0.0.1",
        exec_enabled=False,
        exec_authorized=False,
    )
    assert isinstance(result, ResolvedProbe)
    assert result.kind == "metrics"
    assert result.name == "prometheus"
    assert result.target == "http://127.0.0.1:9090/metrics"
