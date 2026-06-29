"""Session-cached rig health probe + skip gate for integration tests.

Integration tests require the docker-compose.test.yml rig (or `make integration`).
When a required component is unreachable, the test should SKIP FAST (a couple of
seconds) instead of blocking on Rig.boot()'s 30s token budget or a long poll loop.

Design:
  - One httpx GET per component, 2s timeout. A component is healthy iff the GET
    returns a 2xx status. Any connection error / timeout / non-2xx => unhealthy
    (probe_component NEVER raises).
  - Results are cached lazily for the whole pytest session in a module-level dict.
    Each component is probed AT MOST ONCE per worker process, only when a test
    actually needs it.
  - pytest-xdist runs each worker in a SEPARATE PROCESS; the module-level cache is
    therefore per-worker. Each worker probes on-demand (~2s/component worst case).
    This is acceptable and intentional — there is no cross-process shared cache.
  - require_rig_components(*names) probes only the named components (lazily cached)
    and calls pytest.skip(...) immediately if any is unhealthy. This runs BEFORE
    Rig.boot(), so the 30s token budget is never reached when the rig is down.

The gate must be importable WITHOUT a running rig (pure stdlib + httpx + pytest).
"""

from __future__ import annotations

import shutil
import subprocess

import httpx
import pytest

from .rig import RigUrls

PROBE_TIMEOUT_S = 2.0

# Canonical component names. Keep in sync with COMPONENT_HEALTH below.
COMPONENT_NAMES: frozenset[str] = frozenset(
    {
        "monitor",
        "victorialogs",
        "victoriametrics",
        "alertmanager",
        "vmalert-metrics",
        "vmalert-logs",
        "grafana",
        "noisy-logger",
        "fixture-host",
    }
)


def _component_health_targets(urls: RigUrls) -> dict[str, str]:
    """Map canonical component name -> full health-probe URL (base + health path).

    Health paths are taken from docker-compose.test.yml healthchecks (authoritative).
    Monitor uses /api/healthz (router mounted at /api). Grafana is probed DIRECTLY
    (the monitor proxy requires a session cookie and rewrites the path), matching
    test_grafana_provisioning.py.
    """
    return {
        "monitor": f"{urls.monitor}/api/healthz",
        "victorialogs": f"{urls.victorialogs}/health",
        "victoriametrics": f"{urls.victoriametrics}/health",
        "alertmanager": f"{urls.alertmanager}/-/healthy",
        "vmalert-metrics": f"{urls.vmalert_metrics}/health",
        "vmalert-logs": f"{urls.vmalert_logs}/health",
        "grafana": f"{urls.grafana}/api/health",
        "noisy-logger": f"{urls.noisy_logger}/healthz",
        "fixture-host": f"{urls.fixture_host}/healthz",
    }


_HTTP_OK_FLOOR = 200
_HTTP_OK_CEIL = 300

# Module-level session cache (per worker process). Populated lazily on demand.
_health_cache: dict[str, bool] = {}


def probe_component(name: str) -> bool:
    """Probe ONE component with a single httpx GET (2s timeout).

    Returns True iff the health endpoint returns a 2xx status. Any connection
    error, timeout, or non-2xx status returns False. NEVER raises.
    """
    if name not in COMPONENT_NAMES:
        msg = f"unknown rig component {name!r}; known: {sorted(COMPONENT_NAMES)}"
        raise ValueError(msg)
    urls = RigUrls.from_env()
    target = _component_health_targets(urls)[name]
    try:
        resp = httpx.get(target, timeout=PROBE_TIMEOUT_S)
    except httpx.HTTPError:
        return False
    return _HTTP_OK_FLOOR <= resp.status_code < _HTTP_OK_CEIL


def _cached_probe(name: str) -> bool:
    """Probe a component with lazy caching.

    Validates the name, checks the cache, and probes on-demand. Returns True iff
    the component is healthy. Each component is probed AT MOST ONCE per worker
    process.
    """
    if name not in _health_cache:
        _health_cache[name] = probe_component(name)
    return _health_cache[name]


def rig_health() -> dict[str, bool]:
    """Return {name: healthy} for all components, probing each at most once (lazily).

    Subsequent calls in the same process return the cached results without
    re-probing any component.
    """
    return {name: _cached_probe(name) for name in sorted(COMPONENT_NAMES)}


def reset_health_cache() -> None:
    """Clear the session cache. ONLY for the gate's own unit tests."""
    _health_cache.clear()
    _docker_cache.clear()


def require_rig_components(*names: str) -> None:
    """Skip the calling test FAST if any named component is unhealthy.

    Probes only the named components (lazily cached); if any is unhealthy, calls
    pytest.skip(...) immediately (before Rig.boot()). If all are healthy, returns
    and the test proceeds with its normal Rig.boot() 30s token budget.
    """
    downed = [name for name in names if not _cached_probe(name)]
    if downed:
        pytest.skip(
            f"rig component(s) unavailable: {', '.join(sorted(downed))} "
            "-- start the rig via `make integration` (docker-compose.test.yml)"
        )


# --- Docker daemon reachability gate (STAGE-009-003) -------------------------
# The fixer-runner integration test builds + runs a container via the docker CLI
# (not an HTTP endpoint), so it cannot use the httpx-based COMPONENT_NAMES probes
# above. This is a separate, fast (<=2s) daemon-reachability check: `docker
# version` succeeds iff the CLI is on PATH AND the daemon socket answers.

_DOCKER_PROBE_TIMEOUT_S = 2.0
_docker_cache: dict[str, bool] = {}


def docker_available() -> bool:
    """Return True iff the docker CLI is on PATH and the daemon is reachable.

    Cached per worker process. A 2s `docker version` probe; any non-zero exit,
    missing binary, or timeout returns False. NEVER raises.
    """
    if "docker" in _docker_cache:
        return _docker_cache["docker"]
    if shutil.which("docker") is None:
        _docker_cache["docker"] = False
        return False
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        _docker_cache["docker"] = False
        return False
    _docker_cache["docker"] = result.returncode == 0
    return _docker_cache["docker"]


def require_docker() -> None:
    """Skip the calling test FAST if the docker daemon is unreachable.

    Used by docker-driven integration tests (e.g. the fixer-runner build/run
    test) that need the daemon rather than an HTTP rig component.
    """
    if not docker_available():
        pytest.skip(
            "docker daemon unavailable (CLI missing or daemon down) "
            "-- start docker to run this integration test"
        )
