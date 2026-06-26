"""Tests for :class:`SynologyMountHealthCollector` (100% branch coverage).

Filesystem interaction is fully injected: ``mod.present_mounts`` and
``mod.statvfs`` are monkeypatched so no real /proc, no real os.statvfs, and no
wedged executor threads are involved. The "hung" mount is simulated by raising
``TimeoutError`` from the fake ``statvfs`` — the collector handles an
executor-raised ``TimeoutError`` identically to an ``asyncio.wait_for`` timeout,
so this drives the same branch deterministically without a real 5-second wait.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest
import structlog

from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.plugins.collectors.builtin import synology_mount_health as mod
from homelab_monitor.plugins.collectors.builtin.synology_mount_health import (
    M_FREE_BYTES,
    M_MOUNT_UP,
    M_PROBE_SECONDS,
    M_TOTAL_BYTES,
    SynologyMountHealthCollector,
    SynologyMountHealthCollectorConfig,
    parse_mountinfo,
)

# Fake statvfs values (named to avoid PLR2004 in assertions).
_FRSIZE = 4096
_BAVAIL = 100
_BLOCKS = 1000
_EXPECTED_FREE = float(_BAVAIL * _FRSIZE)  # 409600.0
_EXPECTED_TOTAL = float(_BLOCKS * _FRSIZE)  # 4096000.0


class _FakeStatvfs(NamedTuple):
    f_bavail: int
    f_frsize: int
    f_blocks: int


def _ctx(writer: MemoryRetainingMetricsWriter, config: object) -> CollectorContext:
    """Minimal CollectorContext — only the fields the collector reads are real."""
    return CollectorContext(
        config=config,  # pyright: ignore[reportArgumentType]
        db=None,  # pyright: ignore[reportArgumentType]
        vm=writer,
        vl=InMemoryLogsWriter(),
        http=None,  # pyright: ignore[reportArgumentType]
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="synology_mount_health"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[float, dict[str, str]]]:
    """All (value, labels) gauge writes for ``name``, in write order."""
    return [(value, labels) for (n, value, labels) in writer.gauges if n == name]


def _set_present(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    monkeypatch.setattr(mod, "present_mounts", lambda: set(present))


def _ok_statvfs(_path: str) -> _FakeStatvfs:
    return _FakeStatvfs(f_bavail=_BAVAIL, f_frsize=_FRSIZE, f_blocks=_BLOCKS)


# --------------------------------------------------------------------------- #
# Branch 1: empty mount list -> no-op.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_mount_list_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # present_mounts must never even be called; patch to fail loudly if it is.
    def _boom() -> set[str]:
        raise AssertionError("present_mounts should not be called for empty config")

    monkeypatch.setattr(mod, "present_mounts", _boom)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(name="synology_mount_health")
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok
    assert result.metrics_emitted == 0
    assert writer.gauges == []
    assert result.metrics_emitted == len(writer.gauges)


# --------------------------------------------------------------------------- #
# Branch 2: one responsive mount -> seed 0 then overwrite 1, plus probe/free/total.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_responsive_mount_emits_all_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_present(monkeypatch, {"/rackstation"})
    monkeypatch.setattr(mod, "statvfs", _ok_statvfs)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(
        name="synology_mount_health", synology_mounts=["/rackstation"]
    )
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok
    # seed + probe + up + free + total = 5
    assert result.metrics_emitted == 5  # noqa: PLR2004
    assert result.metrics_emitted == len(writer.gauges)

    up = _gauges_named(writer, M_MOUNT_UP)
    # Seed 0.0 THEN overwrite 1.0 both present, in order.
    assert up == [(0.0, {"mount": "/rackstation"}), (1.0, {"mount": "/rackstation"})]

    probe = _gauges_named(writer, M_PROBE_SECONDS)
    assert len(probe) == 1
    probe_value, probe_labels = probe[0]
    assert probe_labels == {"mount": "/rackstation"}
    assert probe_value >= 0.0

    assert _gauges_named(writer, M_FREE_BYTES) == [(_EXPECTED_FREE, {"mount": "/rackstation"})]
    assert _gauges_named(writer, M_TOTAL_BYTES) == [(_EXPECTED_TOTAL, {"mount": "/rackstation"})]
    assert result.errors == []


# --------------------------------------------------------------------------- #
# Branch 3: configured but absent (not in mountinfo) -> seed only.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_absent_mount_emits_seed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_present(monkeypatch, set())  # nothing mounted

    def _statvfs_must_not_run(_path: str) -> _FakeStatvfs:
        raise AssertionError("statvfs must not run for an absent mount")

    monkeypatch.setattr(mod, "statvfs", _statvfs_must_not_run)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(
        name="synology_mount_health", synology_mounts=["/rackstation"]
    )
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok
    assert result.metrics_emitted == 1
    assert result.metrics_emitted == len(writer.gauges)
    assert _gauges_named(writer, M_MOUNT_UP) == [(0.0, {"mount": "/rackstation"})]
    assert _gauges_named(writer, M_PROBE_SECONDS) == []
    assert _gauges_named(writer, M_FREE_BYTES) == []
    assert _gauges_named(writer, M_TOTAL_BYTES) == []
    assert result.errors == []


# --------------------------------------------------------------------------- #
# Branch 4: present but hung (statvfs raises TimeoutError) -> seed + probe, error.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hung_mount_records_error_and_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_present(monkeypatch, {"/rackstation"})

    def _hang(_path: str) -> _FakeStatvfs:
        raise TimeoutError("simulated hung mount")

    monkeypatch.setattr(mod, "statvfs", _hang)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(
        name="synology_mount_health", synology_mounts=["/rackstation"]
    )
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok  # always-True philosophy: a probe round still happened
    assert result.metrics_emitted == 2  # noqa: PLR2004  -- seed + probe
    assert result.metrics_emitted == len(writer.gauges)
    assert _gauges_named(writer, M_MOUNT_UP) == [(0.0, {"mount": "/rackstation"})]
    assert len(_gauges_named(writer, M_PROBE_SECONDS)) == 1
    assert _gauges_named(writer, M_FREE_BYTES) == []
    assert _gauges_named(writer, M_TOTAL_BYTES) == []
    assert len(result.errors) == 1
    assert "/rackstation" in result.errors[0]
    assert "simulated hung mount" in result.errors[0]


# --------------------------------------------------------------------------- #
# Branch 5: present but OSError -> seed + probe, error.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oserror_mount_records_error_and_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_present(monkeypatch, {"/rackstation"})

    def _err(_path: str) -> _FakeStatvfs:
        raise OSError("permission denied")

    monkeypatch.setattr(mod, "statvfs", _err)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(
        name="synology_mount_health", synology_mounts=["/rackstation"]
    )
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok
    assert result.metrics_emitted == 2  # noqa: PLR2004  -- seed + probe
    assert result.metrics_emitted == len(writer.gauges)
    assert _gauges_named(writer, M_MOUNT_UP) == [(0.0, {"mount": "/rackstation"})]
    assert len(_gauges_named(writer, M_PROBE_SECONDS)) == 1
    assert _gauges_named(writer, M_FREE_BYTES) == []
    assert _gauges_named(writer, M_TOTAL_BYTES) == []
    assert len(result.errors) == 1
    assert "permission denied" in result.errors[0]


# --------------------------------------------------------------------------- #
# Branch 6: mixed — responsive + missing + hung, independent verdicts.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mixed_mounts_independent_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    present = {"/rackstation", "/rackstation/Media/TV"}  # /missing not present
    _set_present(monkeypatch, present)

    def _statvfs(path: str) -> _FakeStatvfs:
        if path == "/rackstation":
            return _ok_statvfs(path)
        if path == "/rackstation/Media/TV":
            raise TimeoutError("hung")
        raise AssertionError(f"unexpected statvfs path: {path}")

    monkeypatch.setattr(mod, "statvfs", _statvfs)
    writer = MemoryRetainingMetricsWriter()
    cfg = SynologyMountHealthCollectorConfig(
        name="synology_mount_health",
        synology_mounts=["/rackstation", "/missing", "/rackstation/Media/TV"],
    )
    result = await SynologyMountHealthCollector().run(_ctx(writer, cfg))

    assert result.ok
    # responsive: 5, missing: 1, hung: 2  => 8
    assert result.metrics_emitted == 8  # noqa: PLR2004
    assert result.metrics_emitted == len(writer.gauges)

    up = _gauges_named(writer, M_MOUNT_UP)
    # responsive => seed 0 then 1; missing => seed 0; hung => seed 0.
    assert (1.0, {"mount": "/rackstation"}) in up
    assert (0.0, {"mount": "/rackstation"}) in up
    assert up.count((0.0, {"mount": "/missing"})) == 1
    assert (1.0, {"mount": "/missing"}) not in up
    assert up.count((0.0, {"mount": "/rackstation/Media/TV"})) == 1
    assert (1.0, {"mount": "/rackstation/Media/TV"}) not in up

    # free/total only for the responsive mount.
    assert _gauges_named(writer, M_FREE_BYTES) == [(_EXPECTED_FREE, {"mount": "/rackstation"})]
    assert _gauges_named(writer, M_TOTAL_BYTES) == [(_EXPECTED_TOTAL, {"mount": "/rackstation"})]

    # one error from the hung mount.
    assert len(result.errors) == 1
    assert "/rackstation/Media/TV" in result.errors[0]


# --------------------------------------------------------------------------- #
# Branch 7: parse_mountinfo pure-function branches.
# --------------------------------------------------------------------------- #
def test_parse_mountinfo_extracts_targets_including_nested() -> None:
    text = (
        "36 35 98:0 / /rackstation rw,relatime shared:1 - nfs4 srv:/vol rw\n"
        "37 36 98:0 / /rackstation/Media/TV rw,relatime shared:2 - nfs4 srv:/tv rw\n"
    )
    targets = parse_mountinfo(text)
    assert targets == {"/rackstation", "/rackstation/Media/TV"}


def test_parse_mountinfo_skips_malformed_line() -> None:
    text = (
        "too few fields\n"  # < 5 fields -> skipped
        "36 35 98:0 / /rackstation rw - nfs4 srv:/vol rw\n"  # valid
    )
    targets = parse_mountinfo(text)
    assert targets == {"/rackstation"}


def test_parse_mountinfo_empty_input_is_empty_set() -> None:
    assert parse_mountinfo("") == set()


# --------------------------------------------------------------------------- #
# Coverage helper: exercise present_mounts() / read_mountinfo via injection so
# the wrapper lines are covered without reading the real /proc.
# --------------------------------------------------------------------------- #
def test_present_mounts_uses_read_mountinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = "36 35 98:0 / /rackstation rw - nfs4 srv:/vol rw\n"
    monkeypatch.setattr(mod, "read_mountinfo", lambda: sample)
    assert mod.present_mounts() == {"/rackstation"}


def test_read_mountinfo_reads_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tmp_path is a Path; write a fake mountinfo and read it through read_mountinfo.
    fake = tmp_path / "mountinfo"
    fake.write_text("36 35 98:0 / /rackstation rw - nfs4 srv:/vol rw\n")
    text = mod.read_mountinfo(str(fake))
    assert "/rackstation" in text


# --------------------------------------------------------------------------- #
# Real statvfs wrapper smoke (covers the os.statvfs wrapper line on a path that
# always exists). Uses "/" — never hangs, always present.
# --------------------------------------------------------------------------- #
def test_statvfs_wrapper_returns_result() -> None:
    result = mod.statvfs("/")
    assert isinstance(result, os.statvfs_result)
