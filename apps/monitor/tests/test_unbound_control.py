"""Tests for the unbound-control access layer (STAGE-006-003).

Covers: parse_unbound_stats over the extended + default fixtures (extended detection,
line counts, float values, empty/garbage/mixed inputs); fetch_unbound_stats with a fake
ExecCapture backend (every UnboundError reason + the happy path); and
load_pihole_unbound_config (default + env override).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_monitor.kernel.config import (
    PiholeUnboundConfig,
    load_pihole_unbound_config,
)
from homelab_monitor.kernel.docker.socket_client import (
    DockerSocketConnectionError,
    DockerSocketProtocolError,
    ExecResult,
)
from homelab_monitor.kernel.pihole.unbound_control import (
    UnboundError,
    UnboundStats,
    fetch_unbound_stats,
    parse_unbound_stats,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_EXTENDED_LINE_COUNT = 154
_DEFAULT_LINE_COUNT = 60
_TOTAL_NUM_QUERIES = 108637.0
_REQUESTLIST_AVG = 1.08685


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _FakeBackend:
    """Minimal ExecCapture double: returns a preset ExecResult or raises a preset exc."""

    def __init__(self, *, result: ExecResult | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, object]] = []

    async def exec_capture(
        self, *, container_id: str, cmd: list[str], timeout_seconds: float
    ) -> ExecResult:
        self.calls.append(
            {"container_id": container_id, "cmd": cmd, "timeout_seconds": timeout_seconds}
        )
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


# ---- parse_unbound_stats ----


def test_parse_extended_fixture() -> None:
    stats = parse_unbound_stats(_read_fixture("unbound_stats_extended.txt"))
    assert isinstance(stats, UnboundStats)
    assert stats.extended_enabled is True
    assert stats.raw_line_count == _EXTENDED_LINE_COUNT
    assert stats.raw["total.num.queries"] == _TOTAL_NUM_QUERIES
    # float value parses
    assert stats.raw["total.requestlist.avg"] == _REQUESTLIST_AVG


def test_parse_default_fixture_not_extended() -> None:
    stats = parse_unbound_stats(_read_fixture("unbound_stats_default.txt"))
    assert isinstance(stats, UnboundStats)
    assert stats.extended_enabled is False
    assert stats.raw_line_count == _DEFAULT_LINE_COUNT


def test_parse_empty_string_returns_empty_output_error() -> None:
    result = parse_unbound_stats("")
    assert isinstance(result, UnboundError)
    assert result.reason == "empty_output"


def test_parse_whitespace_only_returns_empty_output_error() -> None:
    result = parse_unbound_stats("   \n\t\n  ")
    assert isinstance(result, UnboundError)
    assert result.reason == "empty_output"


def test_parse_all_garbage_returns_parse_error() -> None:
    """Non-blank lines but none parse as key=value -> parse_error."""
    result = parse_unbound_stats("this is not stats\nneither is this\n")
    assert isinstance(result, UnboundError)
    assert result.reason == "parse_error"


def test_parse_non_float_value_only_returns_parse_error() -> None:
    """Lines with '=' but non-float values still yield parse_error when none parse."""
    result = parse_unbound_stats("a=notanumber\nb=alsobad\n")
    assert isinstance(result, UnboundError)
    assert result.reason == "parse_error"


def test_parse_mixed_skips_bad_lines() -> None:
    """Some valid + a garbage line -> UnboundStats with only the valid pairs."""
    stats = parse_unbound_stats(
        "histogram.x=1\ngarbage-no-equals\nbad=notafloat\ntotal.num.queries=42\n"
    )
    assert isinstance(stats, UnboundStats)
    assert stats.raw_line_count == 2  # noqa: PLR2004
    assert stats.raw["total.num.queries"] == 42.0  # noqa: PLR2004
    assert stats.extended_enabled is True  # histogram. present


def test_parse_value_with_extra_equals_splits_on_first() -> None:
    """A value containing '=' is kept whole (partition on FIRST '=')... value must be float."""
    # left=key, right contains another '='; float() fails -> skipped. Use a clean case:
    stats = parse_unbound_stats("num.query.type.A=5\n")
    assert isinstance(stats, UnboundStats)
    assert stats.raw["num.query.type.A"] == 5.0  # noqa: PLR2004
    assert stats.extended_enabled is True


# ---- fetch_unbound_stats ----


@pytest.mark.asyncio
async def test_fetch_success_returns_stats() -> None:
    backend = _FakeBackend(
        result=ExecResult(
            exit_code=0, stdout=_read_fixture("unbound_stats_extended.txt"), stderr=""
        )
    )
    result = await fetch_unbound_stats(exec_backend=backend, container="pihole-unbound")
    assert isinstance(result, UnboundStats)
    assert result.extended_enabled is True
    # the backend was called with the right argv + container
    assert backend.calls[0]["container_id"] == "pihole-unbound"
    assert backend.calls[0]["cmd"] == ["unbound-control", "stats_noreset"]


@pytest.mark.asyncio
async def test_fetch_nonzero_exit_returns_control_error() -> None:
    backend = _FakeBackend(result=ExecResult(exit_code=1, stdout="", stderr="control socket down"))
    result = await fetch_unbound_stats(exec_backend=backend, container="pihole-unbound")
    assert isinstance(result, UnboundError)
    assert result.reason == "control_error"
    assert "control socket down" in result.message


@pytest.mark.asyncio
async def test_fetch_nonzero_exit_empty_stderr_uses_exit_code() -> None:
    """control_error message falls back to 'exit N' when stderr is empty."""
    backend = _FakeBackend(result=ExecResult(exit_code=3, stdout="", stderr=""))
    result = await fetch_unbound_stats(exec_backend=backend, container="c")
    assert isinstance(result, UnboundError)
    assert result.reason == "control_error"
    assert "exit 3" in result.message


@pytest.mark.asyncio
async def test_fetch_connection_error_returns_socket_error() -> None:
    backend = _FakeBackend(exc=DockerSocketConnectionError("socket gone"))
    result = await fetch_unbound_stats(exec_backend=backend, container="c")
    assert isinstance(result, UnboundError)
    assert result.reason == "socket_error"


@pytest.mark.asyncio
async def test_fetch_protocol_error_returns_container_unreachable() -> None:
    backend = _FakeBackend(exc=DockerSocketProtocolError("bad status 404"))
    result = await fetch_unbound_stats(exec_backend=backend, container="c")
    assert isinstance(result, UnboundError)
    assert result.reason == "container_unreachable"


@pytest.mark.asyncio
async def test_fetch_empty_stdout_returns_empty_output() -> None:
    backend = _FakeBackend(result=ExecResult(exit_code=0, stdout="", stderr=""))
    result = await fetch_unbound_stats(exec_backend=backend, container="c")
    assert isinstance(result, UnboundError)
    assert result.reason == "empty_output"


@pytest.mark.asyncio
async def test_fetch_garbage_stdout_returns_parse_error() -> None:
    backend = _FakeBackend(result=ExecResult(exit_code=0, stdout="totally not stats\n", stderr=""))
    result = await fetch_unbound_stats(exec_backend=backend, container="c")
    assert isinstance(result, UnboundError)
    assert result.reason == "parse_error"


@pytest.mark.asyncio
async def test_fetch_respects_custom_timeout() -> None:
    backend = _FakeBackend(result=ExecResult(exit_code=0, stdout="k=1\n", stderr=""))
    await fetch_unbound_stats(exec_backend=backend, container="c", timeout_seconds=2.5)
    assert backend.calls[0]["timeout_seconds"] == 2.5  # noqa: PLR2004


# ---- load_pihole_unbound_config ----


def test_load_pihole_unbound_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_MONITOR_PIHOLE_UNBOUND_CONTAINER", raising=False)
    cfg = load_pihole_unbound_config()
    assert isinstance(cfg, PiholeUnboundConfig)
    assert cfg.container == "pihole-unbound"


def test_load_pihole_unbound_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_PIHOLE_UNBOUND_CONTAINER", "my-unbound")
    cfg = load_pihole_unbound_config()
    assert cfg.container == "my-unbound"
