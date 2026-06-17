"""Tests for the SshProbe framework base (STAGE-017-003).

Covers the full health-metric truth table via a configurable fake SshClientFactory,
deterministic last_success_age via a monkeypatched monotonic clock, the abstract
marker behavior, and ONE happy-path against the REAL loopback ssh_test_server.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import ClassVar

import pytest
import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    MetricEntry,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.ssh.client import AsyncSshClientFactory
from homelab_monitor.kernel.ssh.errors import (
    HostKeyMismatch,
    HostKeyNotPinned,
    SshAuthError,
    SshConnectionRefused,
    SshTimeout,
    SshTransportError,
)
from homelab_monitor.kernel.ssh.params import SshTargetParams
from homelab_monitor.kernel.ssh.probe import ProbeMetric, ProbeOutcome, SshProbe
from homelab_monitor.kernel.ssh.result import SshCommandResult
from tests.ssh.conftest import SshTestServer

# ---------------------------------------------------------------------------
# Test probe + fakes
# ---------------------------------------------------------------------------


class _UptimeishProbe(SshProbe):
    """Concrete probe: parses the echoed stdout into up + one payload metric."""

    name: ClassVar[str] = "uptimeish"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    target_id: ClassVar[str] = "t1"
    command: ClassVar[str] = "echo selector"

    def parse(self, result: SshCommandResult) -> ProbeOutcome:
        # up when the command echoed our selector and exited 0; emit one payload gauge.
        up = result.exit_status == 0 and "selector" in result.stdout
        return ProbeOutcome(
            up=up,
            metrics=[ProbeMetric(name="homelab_uptimeish_value", value=1.0, labels={})],
        )


class _DownProbe(SshProbe):
    """Concrete probe whose parse always reports up=False (reachable but sad)."""

    name: ClassVar[str] = "downish"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=10)
    target_id: ClassVar[str] = "t1"
    command: ClassVar[str] = "anything"

    def parse(self, result: SshCommandResult) -> ProbeOutcome:
        del result
        return ProbeOutcome(up=False, metrics=[])


class _FakeConnection:
    """Fake SshConnection: returns a preconfigured SshCommandResult from run()."""

    def __init__(self, result: SshCommandResult) -> None:
        self._result = result

    async def run(self, command: str = "") -> SshCommandResult:
        del command
        return self._result


class _FakeFactory:
    """Configurable SshClientFactory test double.

    Either yields a fake connection that returns ``result`` from run(), OR raises
    ``error`` on context entry (open()) to exercise each transport-error path.
    """

    def __init__(
        self,
        *,
        result: SshCommandResult | None = None,
        error: SshTransportError | None = None,
    ) -> None:
        self._result = result
        self._error = error

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncGenerator[_FakeConnection, None]:
        del target_id
        if self._error is not None:
            raise self._error
        assert self._result is not None
        yield _FakeConnection(self._result)


def _ctx(writer: InMemoryMetricsWriter, factory: object) -> CollectorContext:
    """Minimal CollectorContext — vm + ssh are real; the rest are stubs."""
    return CollectorContext(
        config=CollectorConfig(name="uptimeish", interval_seconds=30, timeout_seconds=10),
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=factory,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="uptimeish"),  # pyright: ignore[reportArgumentType]
    )


def _gauges(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in writer.recorded if e.name == name]


def _ok_result() -> SshCommandResult:
    return SshCommandResult(stdout="ran: echo selector\n", stderr="", exit_status=0)


# ---------------------------------------------------------------------------
# Abstract marker
# ---------------------------------------------------------------------------


def test_sshprobe_is_abstract_no_required_classvars() -> None:
    """SshProbe sets abstract=True so it imports without name/interval/timeout."""
    assert SshProbe.abstract is True
    # No TypeError was raised at import time; class object exists.
    assert issubclass(SshProbe, BaseCollector)


def test_concrete_probe_with_all_classvars_constructs() -> None:
    """A concrete SshProbe subclass with required ClassVars instantiates fine."""
    probe = _UptimeishProbe()
    assert probe.name == "uptimeish"
    assert probe.target_id == "t1"
    assert "abstract" not in _UptimeishProbe.__dict__  # marker did NOT inherit


def test_concrete_probe_missing_classvar_raises() -> None:
    """Enforcement still fires for a non-abstract subclass missing required ClassVars."""
    with pytest.raises(TypeError):

        class _Broken(SshProbe):  # pyright: ignore[reportUnusedClass]
            target_id: ClassVar[str] = "t1"

            def parse(self, result: SshCommandResult) -> ProbeOutcome:
                del result
                return ProbeOutcome(up=True)


# ---------------------------------------------------------------------------
# Truth table: success paths
# ---------------------------------------------------------------------------


async def test_connected_parse_up_true() -> None:
    """connect+run, parse up=True -> up=1, mismatch=0, duration, age=0, payload, ok=True."""
    writer = InMemoryMetricsWriter()
    factory = _FakeFactory(result=_ok_result())
    result = await _UptimeishProbe().run(_ctx(writer, factory))

    assert result.ok is True
    assert result.errors == []
    up = _gauges(writer, "homelab_ssh_up")
    assert len(up) == 1
    assert up[0].value == 1.0
    assert up[0].labels == {"target": "t1"}

    mismatch = _gauges(writer, "homelab_ssh_host_key_mismatch")
    assert len(mismatch) == 1
    assert mismatch[0].value == 0.0
    assert mismatch[0].labels == {"target": "t1"}

    dur = _gauges(writer, "homelab_ssh_probe_duration_seconds")
    assert len(dur) == 1
    assert dur[0].labels == {"target": "t1", "probe": "uptimeish"}
    assert dur[0].value >= 0.0

    age = _gauges(writer, "homelab_ssh_last_success_age_seconds")
    assert len(age) == 1
    assert age[0].value == 0.0  # first success
    assert age[0].labels == {"target": "t1", "probe": "uptimeish"}

    payload = _gauges(writer, "homelab_uptimeish_value")
    assert len(payload) == 1
    assert payload[0].value == 1.0
    assert payload[0].labels == {}

    # 3 always + age + payload = 5
    assert result.metrics_emitted == 5  # noqa: PLR2004


async def test_connected_parse_up_false_no_prior_success() -> None:
    """connect+run, parse up=False, no prior success -> up=0, NO age, payload, ok=True."""
    writer = InMemoryMetricsWriter()
    factory = _FakeFactory(result=_ok_result())
    result = await _DownProbe().run(_ctx(writer, factory))

    assert result.ok is True
    assert result.errors == []
    assert _gauges(writer, "homelab_ssh_up")[0].value == 0.0
    assert _gauges(writer, "homelab_ssh_host_key_mismatch")[0].value == 0.0
    assert len(_gauges(writer, "homelab_ssh_last_success_age_seconds")) == 0  # no prior success
    # _DownProbe emits no payload metrics; 3 always-on gauges only.
    assert result.metrics_emitted == 3  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Truth table: transport-error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        HostKeyNotPinned("t1", "no pinned host key"),
        SshConnectionRefused("t1", "refused"),
        SshAuthError("t1", "auth failed"),
        SshTimeout("t1", "timed out"),
        SshTransportError("t1", "unknown transport error"),
    ],
)
async def test_non_mismatch_transport_errors(error: SshTransportError) -> None:
    """Each non-mismatch transport error -> up=0, mismatch=0, ok=False, error msg, no payload."""
    writer = InMemoryMetricsWriter()
    factory = _FakeFactory(error=error)
    result = await _UptimeishProbe().run(_ctx(writer, factory))

    assert result.ok is False
    assert result.errors == [str(error)]
    assert _gauges(writer, "homelab_ssh_up")[0].value == 0.0
    assert _gauges(writer, "homelab_ssh_host_key_mismatch")[0].value == 0.0
    assert len(_gauges(writer, "homelab_ssh_probe_duration_seconds")) == 1
    assert len(_gauges(writer, "homelab_uptimeish_value")) == 0  # no payload on error path
    assert len(_gauges(writer, "homelab_ssh_last_success_age_seconds")) == 0  # no prior success
    assert result.metrics_emitted == 3  # noqa: PLR2004


async def test_host_key_mismatch_sets_signal() -> None:
    """HostKeyMismatch -> mismatch=1, up=0, ok=False, no payload."""
    writer = InMemoryMetricsWriter()
    err = HostKeyMismatch("t1", "server host key did not match pinned key")
    factory = _FakeFactory(error=err)
    result = await _UptimeishProbe().run(_ctx(writer, factory))

    assert result.ok is False
    assert result.errors == [str(err)]
    assert _gauges(writer, "homelab_ssh_up")[0].value == 0.0
    mismatch = _gauges(writer, "homelab_ssh_host_key_mismatch")
    assert len(mismatch) == 1
    assert mismatch[0].value == 1.0
    assert mismatch[0].labels == {"target": "t1"}
    assert len(_gauges(writer, "homelab_uptimeish_value")) == 0
    assert result.metrics_emitted == 3  # noqa: PLR2004


# ---------------------------------------------------------------------------
# last_success_age logic (deterministic clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic-clock stub returning preset values in sequence, last value sticky."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        v = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return v


def _patch_clock(monkeypatch: pytest.MonkeyPatch, values: list[float]) -> None:
    monkeypatch.setattr("homelab_monitor.kernel.ssh.probe.time.monotonic", _FakeClock(values))


async def test_age_emitted_zero_on_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """First up=1 run emits age == 0.0 and records the success timestamp."""
    # run() reads monotonic 2x: start + duration-end (age reuses it). All 100.0.
    _patch_clock(monkeypatch, [100.0])
    writer = InMemoryMetricsWriter()
    probe = _UptimeishProbe()
    await probe.run(_ctx(writer, _FakeFactory(result=_ok_result())))
    age = _gauges(writer, "homelab_ssh_last_success_age_seconds")
    assert len(age) == 1
    assert age[0].value == 0.0


async def test_age_grows_then_resets_across_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Age 0 on first success, > 0 on later non-up run, resets to 0 on a later success."""
    writer = InMemoryMetricsWriter()
    probe = _UptimeishProbe()  # reused across ticks (scheduler behavior)

    # Run 1: success at t=100 -> age 0, last_success=100. Clock sticky at 100.
    _patch_clock(monkeypatch, [100.0])
    await probe.run(_ctx(writer, _FakeFactory(result=_ok_result())))

    # Run 2: same instance goes non-up by feeding a non-selector result (parse -> up=False)
    _patch_clock(monkeypatch, [150.0])
    bad = SshCommandResult(stdout="nope\n", stderr="", exit_status=0)
    writer2 = InMemoryMetricsWriter()
    await probe.run(_ctx(writer2, _FakeFactory(result=bad)))
    age2 = _gauges(writer2, "homelab_ssh_last_success_age_seconds")
    assert len(age2) == 1
    assert age2[0].value == 50.0  # 150 - 100  # noqa: PLR2004
    assert _gauges(writer2, "homelab_ssh_up")[0].value == 0.0

    # Run 3: success again at t=160 -> age 0 (reset), last_success=160.
    _patch_clock(monkeypatch, [160.0])
    writer3 = InMemoryMetricsWriter()
    await probe.run(_ctx(writer3, _FakeFactory(result=_ok_result())))
    age3 = _gauges(writer3, "homelab_ssh_last_success_age_seconds")
    assert len(age3) == 1
    assert age3[0].value == 0.0


async def test_age_emitted_on_transport_error_after_prior_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error AFTER a prior success still emits age = now - last_success."""
    writer = InMemoryMetricsWriter()
    probe = _UptimeishProbe()

    _patch_clock(monkeypatch, [200.0])
    await probe.run(_ctx(writer, _FakeFactory(result=_ok_result())))

    _patch_clock(monkeypatch, [275.0])
    writer2 = InMemoryMetricsWriter()
    await probe.run(_ctx(writer2, _FakeFactory(error=SshTimeout("t1", "timed out"))))
    age = _gauges(writer2, "homelab_ssh_last_success_age_seconds")
    assert len(age) == 1
    assert age[0].value == 75.0  # 275 - 200  # noqa: PLR2004
    # transport-error run: up=0, ok=False, age emitted -> 4 gauges total
    assert _gauges(writer2, "homelab_ssh_up")[0].value == 0.0


# ---------------------------------------------------------------------------
# Real loopback (end-to-end with the real transport)
# ---------------------------------------------------------------------------


async def test_happy_path_against_real_loopback(ssh_test_server: SshTestServer) -> None:
    """End-to-end: real AsyncSshClientFactory + loopback server -> up=1, ok=True."""
    params = SshTargetParams(
        host="127.0.0.1",
        port=ssh_test_server.port,
        user="tester",
        key_secret_name="ssh_key",
        pinned_host_key=ssh_test_server.host_pubkey_line,
        account_mode="dedicated_user",
    )
    factory = AsyncSshClientFactory(
        resolve=lambda tid: params if tid == "t1" else None,
        secrets_for=lambda name: ssh_test_server.client_key_pem if name == "ssh_key" else None,
    )
    writer = InMemoryMetricsWriter()
    result = await _UptimeishProbe().run(_ctx(writer, factory))

    assert result.ok is True
    assert _gauges(writer, "homelab_ssh_up")[0].value == 1.0
    assert _gauges(writer, "homelab_ssh_host_key_mismatch")[0].value == 0.0
    payload = _gauges(writer, "homelab_uptimeish_value")
    assert len(payload) == 1  # the loopback echoes "ran: echo selector" -> selector present
