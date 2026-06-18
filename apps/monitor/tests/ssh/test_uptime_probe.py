"""Tests for the config-driven UptimeProbe exemplar (STAGE-017-006).

Covers:
- parse() pure logic (all branches: ok, nonzero exit, empty stdout, garbage stdout)
- make_uptime_probe() factory (ClassVars set correctly, abstract=False, subclass check)
- lifecycle against real loopback (graceful degradation when forced-command returns
  non-float stdout)
- PluginLoader registration (uptime-t1 registered with interval 60s)
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext

# Re-use helpers defined in test_ssh_probe.py  ← NO: they are module-private.
# Re-define the minimal set here (same pattern).
from homelab_monitor.kernel.plugins.io import InMemoryLogsWriter, InMemoryMetricsWriter, MetricEntry
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
from homelab_monitor.kernel.ssh.params import SshTargetParams
from homelab_monitor.kernel.ssh.probe import SshProbe
from homelab_monitor.kernel.ssh.result import SshCommandResult
from homelab_monitor.plugins.collectors.ssh import register_all
from homelab_monitor.plugins.collectors.ssh.uptime import UptimeProbe, make_uptime_probe
from tests.ssh.conftest import SshTestServer

# ---------------------------------------------------------------------------
# Helpers (mirror test_ssh_probe.py pattern; private to this module)
# ---------------------------------------------------------------------------


def _ctx(writer: InMemoryMetricsWriter, factory: object) -> CollectorContext:
    return CollectorContext(
        config=CollectorConfig(name="uptime-t1", interval_seconds=60, timeout_seconds=10),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=factory,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="uptime-t1"),  # pyright: ignore[reportArgumentType]
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in writer.recorded if e.name == name]


# ---------------------------------------------------------------------------
# parse() pure tests (no server needed)
# ---------------------------------------------------------------------------


def test_parse_ok() -> None:
    """/proc/uptime happy path: first token parsed as float uptime seconds."""
    probe = make_uptime_probe("t1")()
    result = SshCommandResult(stdout="2150060.91 29871962.63\n", stderr="", exit_status=0)
    outcome = probe.parse(result)

    assert outcome.up is True
    assert len(outcome.metrics) == 1
    metric = outcome.metrics[0]
    assert metric.name == "homelab_ssh_uptime_seconds"
    assert metric.value == 2150060.91  # noqa: PLR2004
    assert metric.labels == {"target": "t1"}


def test_parse_nonzero_exit() -> None:
    """Non-zero exit status -> up=False, no metrics."""
    probe = make_uptime_probe("t1")()
    result = SshCommandResult(stdout="2150060.91 29871962.63\n", stderr="error\n", exit_status=1)
    outcome = probe.parse(result)

    assert outcome.up is False
    assert outcome.metrics == []


def test_parse_empty_stdout() -> None:
    """Empty stdout with exit 0 -> IndexError path -> up=False, no metrics."""
    probe = make_uptime_probe("t1")()
    result = SshCommandResult(stdout="", stderr="", exit_status=0)
    outcome = probe.parse(result)

    assert outcome.up is False
    assert outcome.metrics == []


def test_parse_garbage_stdout() -> None:
    """Non-float first token -> ValueError path -> up=False, no metrics."""
    probe = make_uptime_probe("t1")()
    result = SshCommandResult(stdout="ran: cat /proc/uptime\n", stderr="", exit_status=0)
    outcome = probe.parse(result)

    assert outcome.up is False
    assert outcome.metrics == []


# ---------------------------------------------------------------------------
# make_uptime_probe() factory
# ---------------------------------------------------------------------------


def test_factory_sets_classvars() -> None:
    """make_uptime_probe synthesizes correct ClassVars and remains a UptimeProbe subclass."""
    cls = make_uptime_probe("foo")

    assert cls.target_id == "foo"
    assert cls.name == "uptime-foo"
    assert cls.concurrency_group == "ssh_foo"
    assert cls.abstract is False
    assert issubclass(cls, UptimeProbe)
    assert issubclass(cls, SshProbe)
    # Inherited ClassVars
    assert cls.command == "cat /proc/uptime"
    assert cls.interval == timedelta(seconds=60)
    assert cls.timeout == timedelta(seconds=10)

    # parse works on the concrete instance
    outcome = cls().parse(SshCommandResult(stdout="500.0 1000.0\n", stderr="", exit_status=0))
    assert outcome.up is True
    assert outcome.metrics[0].value == 500.0  # noqa: PLR2004
    assert outcome.metrics[0].labels == {"target": "foo"}


def test_base_uptime_probe_is_abstract() -> None:
    """UptimeProbe sets abstract=True so it imports/exists without enforcement triggering."""
    assert UptimeProbe.abstract is True
    assert issubclass(UptimeProbe, SshProbe)


# ---------------------------------------------------------------------------
# PluginLoader registration
# ---------------------------------------------------------------------------


def test_register_uptime_probe() -> None:
    """make_uptime_probe + config_from_classvars registers successfully; load_all returns it."""
    loader = PluginLoader(log=structlog.get_logger())  # pyright: ignore[reportArgumentType]
    cls = make_uptime_probe("t1")
    loader.register(cls, config_from_classvars(cls))

    loaded = loader.load_all()
    names = [lc.config.name for lc in loaded]
    assert "uptime-t1" in names

    uptime_entry = next(lc for lc in loaded if lc.config.name == "uptime-t1")
    assert uptime_entry.config.interval_seconds == 60  # noqa: PLR2004


def test_register_all_registers_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_all loops over configured targets and registers one probe each."""
    monkeypatch.setattr(
        "homelab_monitor.plugins.collectors.ssh.load_ssh_target_configs",
        lambda: {"t1": object(), "t2": object()},
    )
    loader = PluginLoader(log=structlog.get_logger())  # pyright: ignore[reportArgumentType]
    register_all(loader)

    names = {lc.config.name for lc in loader.load_all()}
    assert "uptime-t1" in names
    assert "uptime-t2" in names


def test_register_all_isolates_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_all swallows per-target registration errors without re-raising."""
    monkeypatch.setattr(
        "homelab_monitor.plugins.collectors.ssh.load_ssh_target_configs",
        lambda: {"t1": object(), "t2": object()},
    )

    calls: list[str] = []

    class _FailingLoader:
        def register(self, cls: object, cfg: object) -> None:
            calls.append(getattr(cls, "target_id", "?"))
            raise RuntimeError("boom")

    register_all(_FailingLoader())  # pyright: ignore[reportArgumentType]

    # Both targets attempted; exception was swallowed, not propagated.
    assert calls == ["t1", "t2"]


# ---------------------------------------------------------------------------
# Lifecycle: loopback with forced-command server (graceful degradation)
# ---------------------------------------------------------------------------


async def test_lifecycle_emit_surface_graceful_degradation(
    ssh_test_server_forced_command: SshTestServer,
) -> None:
    """End-to-end against forced-command server.

    The forced-command server returns ``ran: echo HM_FORCED_OK\\n``. The first
    token ``ran:`` is not a float, so parse() returns up=False. The framework
    still emits the 3 always-on SSH health gauges; ``homelab_ssh_uptime_seconds``
    is ABSENT because parse failed gracefully.
    """
    server = ssh_test_server_forced_command
    params = SshTargetParams(
        host="127.0.0.1",
        port=server.port,
        user="tester",
        key_secret_name="ssh_key",
        pinned_host_key=server.host_pubkey_line,
        account_mode="dedicated_user",
    )
    factory = AsyncSshClientFactory(
        resolve=lambda tid: params if tid == "t1" else None,
        secrets_for=lambda name: server.client_key_pem if name == "ssh_key" else None,
    )
    writer = InMemoryMetricsWriter()
    probe = make_uptime_probe("t1")()
    result = await probe.run(_ctx(writer, factory))

    # Connection succeeded; parse failed gracefully -> ok=True (transport ok)
    assert result.ok is True

    # Always-on gauges present
    up_gauges = _gauges(writer, "homelab_ssh_up")
    assert len(up_gauges) == 1
    assert up_gauges[0].value == 0.0  # parse returned up=False

    assert len(_gauges(writer, "homelab_ssh_probe_duration_seconds")) == 1
    assert len(_gauges(writer, "homelab_ssh_host_key_mismatch")) == 1
    assert _gauges(writer, "homelab_ssh_host_key_mismatch")[0].value == 0.0

    # Payload metric absent (parse failed)
    assert len(_gauges(writer, "homelab_ssh_uptime_seconds")) == 0
