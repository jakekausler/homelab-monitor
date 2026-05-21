"""Tests for ``hm dev seed-container-metrics`` (STAGE-003-001)."""

from __future__ import annotations

import argparse
import random

import httpx
import pytest
from pytest_httpx import HTTPXMock

from homelab_monitor.cli import dev as dev_cli
from homelab_monitor.cli.dev import (
    _build_exposition,  # pyright: ignore[reportPrivateUsage]
    _cmd_seed_container_metrics,  # pyright: ignore[reportPrivateUsage]
    _handle,  # pyright: ignore[reportPrivateUsage]
    _resolve_vm_url,  # pyright: ignore[reportPrivateUsage]
    _shape_values,  # pyright: ignore[reportPrivateUsage]
)

LOCAL_HOST = "local-test-host"
VM_URL = "http://vm-test:8428"


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


class TestHandleDispatch:
    def test_dispatch_seed_container_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_handle dispatches dev_cmd='seed-container-metrics' to the impl."""
        called: list[tuple[int, bool, bool, str | None]] = []

        async def fake_seed(
            *,
            containers: int,
            clear: bool,
            force: bool,
            vm_url: str | None,
        ) -> int:
            called.append((containers, clear, force, vm_url))
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_seed_container_metrics", fake_seed)
        args = argparse.Namespace(
            dev_cmd="seed-container-metrics",
            containers=3,
            clear=False,
            force=False,
            vm_url=None,
        )
        rc = _handle(args)
        assert rc == 0
        assert called == [(3, False, False, None)]

    def test_dispatch_clear_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--clear flag flows through to the impl."""
        called: list[bool] = []

        async def fake_seed(
            *,
            containers: int,
            clear: bool,
            force: bool,
            vm_url: str | None,
        ) -> int:
            called.append(clear)
            return 0

        monkeypatch.setattr(dev_cli, "_cmd_seed_container_metrics", fake_seed)
        args = argparse.Namespace(
            dev_cmd="seed-container-metrics",
            containers=5,
            clear=True,
            force=False,
            vm_url=None,
        )
        rc = _handle(args)
        assert rc == 0
        assert called == [True]

    def test_missing_subcommand_message_lists_new_command(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """_handle without dev_cmd prints usage mentioning seed-container-metrics."""
        args = argparse.Namespace()
        rc = _handle(args)
        assert rc == 2  # noqa: PLR2004
        captured = capsys.readouterr()
        assert "seed-container-metrics" in captured.err


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestResolveVmUrl:
    def test_flag_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://from-env:8428")
        assert _resolve_vm_url("http://from-flag:8428") == "http://from-flag:8428"

    def test_env_wins_when_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOMELAB_MONITOR_VM_URL", "http://from-env:8428")
        assert _resolve_vm_url(None) == "http://from-env:8428"

    def test_flag_used_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOMELAB_MONITOR_VM_URL", raising=False)
        assert _resolve_vm_url("http://from-flag:8428") == "http://from-flag:8428"

    def test_default_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOMELAB_MONITOR_VM_URL", raising=False)
        assert _resolve_vm_url(None) == "http://127.0.0.1:18428"

    def test_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOMELAB_MONITOR_VM_URL", raising=False)
        assert _resolve_vm_url("http://from-flag:8428/") == "http://from-flag:8428"


# ---------------------------------------------------------------------------
# Hostname gate
# ---------------------------------------------------------------------------


@pytest.fixture
def local_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the dev-CLI module's resolve_hostname to a known local host."""

    def _hostname() -> str:
        return LOCAL_HOST

    monkeypatch.setattr("homelab_monitor.cli.dev.socket.gethostname", _hostname)


@pytest.mark.asyncio
async def test_seed_refuses_remote_without_force(
    local_dev_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """HM_HOST_HOSTNAME != resolve_hostname() and no --force => exit 1."""
    monkeypatch.setenv("HM_HOST_HOSTNAME", "other-host")
    rc = await _cmd_seed_container_metrics(containers=3, clear=False, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "hostname mismatch" in captured.err


@pytest.mark.asyncio
async def test_seed_accepts_remote_with_force(
    local_dev_env: None,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    """--force overrides the hostname mismatch."""
    monkeypatch.setenv("HM_HOST_HOSTNAME", "other-host")
    httpx_mock.add_response(
        url=f"{VM_URL}/api/v1/import/prometheus",
        method="POST",
        status_code=204,
    )
    rc = await _cmd_seed_container_metrics(containers=3, clear=False, force=True, vm_url=VM_URL)
    assert rc == 0


# ---------------------------------------------------------------------------
# Shape generation
# ---------------------------------------------------------------------------


class TestShapeValues:
    def test_idle_shape_is_low_everywhere(self) -> None:
        rng = random.Random("test")
        cpu, mem, rx, tx = _shape_values("idle", rng, idx=0)
        assert cpu < 1.0
        assert mem < 100 * 1024 * 1024
        assert rx < 1_000  # noqa: PLR2004
        assert tx < 1_000  # noqa: PLR2004

    def test_unknown_shape_raises(self) -> None:
        rng = random.Random("test")
        with pytest.raises(ValueError, match="unknown shape"):
            _shape_values("not-a-shape", rng, idx=0)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestExpositionDeterminism:
    def test_same_minute_same_count_yields_same_payload(self) -> None:
        # epoch_a and epoch_b are in the same minute (differ by < 60).
        epoch_a = 1_715_000_000
        epoch_b = epoch_a + 30  # same minute bucket
        payload_a = _build_exposition(5, epoch_a)
        payload_b = _build_exposition(5, epoch_b)

        # Strip the `container_last_seen` lines because they embed the raw
        # epoch and intentionally vary within the minute.
        def _strip_last_seen(p: str) -> list[str]:
            return [ln for ln in p.splitlines() if not ln.startswith("container_last_seen")]

        assert _strip_last_seen(payload_a) == _strip_last_seen(payload_b)

    def test_different_minutes_yield_different_payload(self) -> None:
        epoch_a = 1_715_000_000
        epoch_b = epoch_a + 600  # 10 minutes later -> different minute bucket
        payload_a = _build_exposition(5, epoch_a)
        payload_b = _build_exposition(5, epoch_b)
        assert payload_a != payload_b

    def test_payload_includes_synthetic_label(self) -> None:
        payload = _build_exposition(3, 1_715_000_000)
        assert 'homelab_synthetic="true"' in payload
        # Three synthetic containers => the label appears in every series row;
        # 5 series families per container * 3 = 15+ occurrences.
        assert payload.count('homelab_synthetic="true"') >= 15  # noqa: PLR2004

    def test_payload_uses_distinct_shapes(self) -> None:
        payload = _build_exposition(5, 1_715_000_000)
        for shape in ("cpu-sawtooth", "memory-step", "network-bursts", "idle", "spiky"):
            assert f'shape="{shape}"' in payload

    def test_payload_cycles_shapes_for_n_gt_5(self) -> None:
        payload = _build_exposition(7, 1_715_000_000)
        # idx=5 reuses shape index 0 = cpu-sawtooth; idx=6 reuses shape 1 = memory-step.
        assert 'name="hm-synth-5"' in payload
        assert 'name="hm-synth-6"' in payload


# ---------------------------------------------------------------------------
# HTTP integration with mocked VM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_posts_to_vm_import_endpoint(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
) -> None:
    """Seed mode POSTs exposition data to /api/v1/import/prometheus."""
    httpx_mock.add_response(
        url=f"{VM_URL}/api/v1/import/prometheus",
        method="POST",
        status_code=204,
    )
    rc = await _cmd_seed_container_metrics(containers=5, clear=False, force=False, vm_url=VM_URL)
    assert rc == 0

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL(f"{VM_URL}/api/v1/import/prometheus")
    body = request.content.decode("utf-8")
    assert 'name="hm-synth-0"' in body
    assert 'name="hm-synth-4"' in body
    assert 'homelab_synthetic="true"' in body


@pytest.mark.asyncio
async def test_seed_returns_1_on_vm_error(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """500 from VM => exit 1 with error message."""
    httpx_mock.add_response(
        url=f"{VM_URL}/api/v1/import/prometheus",
        method="POST",
        status_code=500,
        text="vm exploded",
    )
    rc = await _cmd_seed_container_metrics(containers=5, clear=False, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "VM rejected import" in captured.err


@pytest.mark.asyncio
async def test_seed_returns_1_on_vm_unreachable(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Connection error => exit 1 with 'VM unreachable'."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    rc = await _cmd_seed_container_metrics(containers=5, clear=False, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "VM unreachable" in captured.err


@pytest.mark.asyncio
async def test_seed_rejects_zero_containers(
    local_dev_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--containers 0 => exit 1 with error before any HTTP call."""
    rc = await _cmd_seed_container_metrics(containers=0, clear=False, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "containers must be >= 1" in captured.err


@pytest.mark.asyncio
async def test_clear_posts_to_delete_series(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
) -> None:
    """--clear POSTs to /api/v1/admin/tsdb/delete_series with the synthetic matcher."""
    httpx_mock.add_response(
        url=(
            f"{VM_URL}/api/v1/admin/tsdb/delete_series"
            "?match%5B%5D=%7Bhomelab_synthetic%3D%22true%22%7D"
        ),
        method="POST",
        status_code=204,
    )
    rc = await _cmd_seed_container_metrics(containers=5, clear=True, force=False, vm_url=VM_URL)
    assert rc == 0
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert "/api/v1/admin/tsdb/delete_series" in str(requests[0].url)
    assert 'match[]={homelab_synthetic="true"}' in requests[0].url.params.get("match[]", "") or (
        # httpx URL params normalize the key; check the raw query string.
        "homelab_synthetic" in str(requests[0].url)
    )


@pytest.mark.asyncio
async def test_clear_returns_1_on_vm_error(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """500 from VM on clear => exit 1."""
    httpx_mock.add_response(
        url=(
            f"{VM_URL}/api/v1/admin/tsdb/delete_series"
            "?match%5B%5D=%7Bhomelab_synthetic%3D%22true%22%7D"
        ),
        method="POST",
        status_code=500,
        text="boom",
    )
    rc = await _cmd_seed_container_metrics(containers=5, clear=True, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "VM rejected delete_series" in captured.err


@pytest.mark.asyncio
async def test_clear_returns_1_on_vm_unreachable(
    local_dev_env: None,
    httpx_mock: HTTPXMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Connection error during --clear => exit 1 with 'VM unreachable'."""
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    rc = await _cmd_seed_container_metrics(containers=5, clear=True, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "VM unreachable" in captured.err


@pytest.mark.asyncio
async def test_seed_refuses_when_env_hostname_differs_from_os(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gate rejects when HM_HOST_HOSTNAME differs from socket.gethostname()."""
    # Do NOT use local_dev_env fixture — let socket.gethostname() return the real value.
    monkeypatch.setenv("HM_HOST_HOSTNAME", "definitely-not-this-machine")
    rc = await _cmd_seed_container_metrics(containers=3, clear=False, force=False, vm_url=VM_URL)
    assert rc == 1
    captured = capsys.readouterr()
    assert "hostname mismatch" in captured.err
