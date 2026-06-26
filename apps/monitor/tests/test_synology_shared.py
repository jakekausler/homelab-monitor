"""Unit tests for plugins/collectors/integrations/synology/_shared.py.

100% branch coverage of _shared.py.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext

from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.io import MemoryRetainingMetricsWriter
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError, SynologyErrorReason
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    M_API_TOOK_SECONDS,
    as_float,
    bytes_field,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    failed_result,
    fetch_or_result,
    percent_field,
)

# ---------------------------------------------------------------------------
# Fake CollectorContext (minimal stand-in — mirrors unifi/HA collector tests)
# ---------------------------------------------------------------------------


def _ctx() -> SimpleNamespace:
    """Build a partial CollectorContext as a SimpleNamespace."""
    return SimpleNamespace(
        vm=MemoryRetainingMetricsWriter(),
        synology=None,
    )


def _synology_resp(
    *,
    payload: object = None,
    took_seconds: float = 0.42,
    endpoint: str = "SYNO.Core.System/info",
) -> SynologyResponse:
    return SynologyResponse(
        payload=payload if payload is not None else {"model": "DS3622xs+"},
        took_seconds=took_seconds,
        endpoint=endpoint,
    )


def _synology_err(
    reason: SynologyErrorReason = "unreachable",
    message: str = "connection refused",
    status: int | None = None,
) -> SynologyError:
    return SynologyError(reason=reason, message=message, status=status)


# ---------------------------------------------------------------------------
# client_unconfigured_result
# ---------------------------------------------------------------------------


def test_client_unconfigured_result_fields() -> None:
    start = time.monotonic()
    result = client_unconfigured_result(start)
    assert isinstance(result, CollectorResult)
    assert result.ok is False
    assert result.metrics_emitted == 0
    assert result.errors == ["synology client not configured"]
    assert result.events == []
    assert result.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# failed_result
# ---------------------------------------------------------------------------


def test_failed_result_unreachable() -> None:
    err = _synology_err(reason="unreachable", message="host not reachable")
    start = time.monotonic()
    result = failed_result(err, start)
    assert result.ok is False
    assert result.errors == ["host not reachable"]
    assert result.metrics_emitted == 0
    assert result.events == []
    assert result.duration_seconds >= 0.0


def test_failed_result_api_error_with_status() -> None:
    err = _synology_err(reason="api_error", message="DSM error 400", status=400)
    start = time.monotonic()
    result = failed_result(err, start)
    assert result.ok is False
    assert result.errors == ["DSM error 400"]
    assert result.metrics_emitted == 0


# ---------------------------------------------------------------------------
# fetch_or_result — success branch
# ---------------------------------------------------------------------------


def test_fetch_or_result_success_emits_took_gauge() -> None:
    ctx = cast("CollectorContext", _ctx())
    _took_seconds = 0.42
    resp = _synology_resp(took_seconds=_took_seconds, endpoint="SYNO.Core.System/info")
    emitted: list[int] = [0]
    start = time.monotonic()

    returned = fetch_or_result(ctx, resp, start, emitted)

    assert returned is resp
    assert emitted[0] == 1

    gauges = ctx.vm.gauges  # type: ignore[attr-defined]  # MemoryRetainingMetricsWriter stores gauges
    assert len(gauges) == 1  # type: ignore[arg-type]
    name, value, labels = gauges[0]  # type: ignore[misc]
    assert name == M_API_TOOK_SECONDS
    assert value == pytest.approx(_took_seconds)  # type: ignore[no-untyped-call]
    assert labels == {"api": "SYNO.Core.System/info"}


def test_fetch_or_result_success_accumulates_emitted() -> None:
    ctx = cast("CollectorContext", _ctx())
    resp = _synology_resp()
    _initial_count = 5
    emitted: list[int] = [_initial_count]  # pre-existing count
    fetch_or_result(ctx, resp, time.monotonic(), emitted)
    assert emitted[0] == _initial_count + 1


# ---------------------------------------------------------------------------
# fetch_or_result — error branch
# ---------------------------------------------------------------------------


def test_fetch_or_result_error_returns_collector_result() -> None:
    ctx = cast("CollectorContext", _ctx())
    err = _synology_err(reason="timeout", message="timed out")
    emitted: list[int] = [0]
    start = time.monotonic()

    returned = fetch_or_result(ctx, err, start, emitted)

    assert isinstance(returned, CollectorResult)
    assert returned.ok is False
    assert returned.errors == ["timed out"]
    assert emitted[0] == 0
    assert ctx.vm.gauges == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# as_float
# ---------------------------------------------------------------------------

_INT_42 = 42
_FLOAT_314 = 3.14
_FLOAT_25 = 2.5
_FLOAT_7 = 7.0


@pytest.mark.parametrize(
    "v,expected",
    [
        (_INT_42, 42.0),
        (_FLOAT_314, _FLOAT_314),
        ("2.5", _FLOAT_25),
        (" 7 ", _FLOAT_7),
        (True, None),  # bool rejected before int
        (False, None),  # bool rejected before int
        (None, None),
        ("hello", None),
        ("", None),
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        ("inf", None),
        ("-inf", None),
        ([], None),
    ],
)
def test_as_float(v: object, expected: float | None) -> None:
    result = as_float(v)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# bytes_field
# ---------------------------------------------------------------------------


def test_bytes_field_valid() -> None:
    _gb = 1073741824
    assert bytes_field(_gb) == pytest.approx(float(_gb))  # type: ignore[no-untyped-call]


def test_bytes_field_none() -> None:
    assert bytes_field("not-a-number") is None


# ---------------------------------------------------------------------------
# percent_field
# ---------------------------------------------------------------------------


def test_percent_field_valid() -> None:
    _percent = 75.5
    assert percent_field(str(_percent)) == pytest.approx(_percent)  # type: ignore[no-untyped-call]


def test_percent_field_none() -> None:
    assert percent_field(None) is None


# ---------------------------------------------------------------------------
# cap_for_synology
# ---------------------------------------------------------------------------


def test_cap_for_synology_unlisted_family_returns_default() -> None:
    # No synology families defined in cardinality config yet; default is 500
    _default_cap = 500
    cap = cap_for_synology("homelab_synology_disk_temp_celsius")
    assert cap == _default_cap


# ---------------------------------------------------------------------------
# capped_emitter
# ---------------------------------------------------------------------------


def test_capped_emitter_wires_writer_and_events() -> None:
    ctx = cast("CollectorContext", _ctx())
    events: list[Any] = []
    emitter = capped_emitter(ctx, events)

    assert isinstance(emitter, CappedEmitter)
    assert emitter.writer is ctx.vm  # type: ignore[union-attr]
    assert emitter.events is events


def test_capped_emitter_round_trip_emit_family() -> None:
    ctx = cast("CollectorContext", _ctx())
    events: list[Any] = []
    emitter = capped_emitter(ctx, events)

    _cap = 500
    obs = [
        ({"disk": "sda"}, 42.0),
        ({"disk": "sdb"}, 38.0),
    ]
    survivors = emitter.emit_family("homelab_synology_disk_temp_celsius", _cap, obs)
    assert survivors == len(obs)
    # Both observations emitted + the always-written drop gauge
    assert len(ctx.vm.gauges) >= len(obs)  # type: ignore[attr-defined]
