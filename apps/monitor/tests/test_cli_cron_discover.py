"""Tests for 'hm cron discover' CLI command (STAGE-002-007)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homelab_monitor.cli.cron import _cmd_discover  # pyright: ignore[reportPrivateUsage]
from homelab_monitor.kernel.cron.discovery_types import CronScanError, CronScanResult


@pytest.mark.asyncio
async def test_cron_discover_returns_0_on_success() -> None:
    """'hm cron discover' returns exit code 0 when partial=False."""
    mock_result = CronScanResult(
        found_fingerprints=frozenset(["fp1", "fp2"]),
        inserted_count=1,
        updated_count=1,
        bump_only_count=0,
        partial=False,
        errors=[],
    )

    with (
        patch("homelab_monitor.cli.cron.CronDiscoverer") as mock_discoverer_class,
        patch("homelab_monitor.cli.cron.get_engine"),
        patch("homelab_monitor.cli.cron.SqliteRepository"),
        patch("homelab_monitor.cli.cron.CronRepo"),
        patch("builtins.print") as mock_print,
    ):
        mock_discoverer = AsyncMock()
        mock_discoverer.scan = AsyncMock(return_value=mock_result)
        mock_discoverer_class.return_value = mock_discoverer

        rc = await _cmd_discover()
        assert rc == 0

        # Verify output contains the expected fields
        output_call = [call for call in mock_print.call_args_list if "discovered:" in str(call)]
        assert len(output_call) > 0


@pytest.mark.asyncio
async def test_cron_discover_returns_1_on_partial() -> None:
    """'hm cron discover' returns exit code 1 when partial=True."""
    mock_result = CronScanResult(
        found_fingerprints=frozenset(["fp1"]),
        inserted_count=1,
        updated_count=0,
        bump_only_count=0,
        partial=True,
        errors=[],
    )

    with (
        patch("homelab_monitor.cli.cron.CronDiscoverer") as mock_discoverer_class,
        patch("homelab_monitor.cli.cron.get_engine"),
        patch("homelab_monitor.cli.cron.SqliteRepository"),
        patch("homelab_monitor.cli.cron.CronRepo"),
        patch("builtins.print"),
    ):
        mock_discoverer = AsyncMock()
        mock_discoverer.scan = AsyncMock(return_value=mock_result)
        mock_discoverer_class.return_value = mock_discoverer

        rc = await _cmd_discover()
        assert rc == 1


@pytest.mark.asyncio
async def test_cron_discover_prints_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """'hm cron discover' prints errors to stderr."""
    mock_result = CronScanResult(
        found_fingerprints=frozenset(["fp1"]),
        inserted_count=1,
        updated_count=0,
        bump_only_count=0,
        partial=True,
        errors=[
            CronScanError(host_source_path="/etc/cron.d/broken", error="Parse error at line 1"),
        ],
    )

    with (
        patch("homelab_monitor.cli.cron.CronDiscoverer") as mock_discoverer_class,
        patch("homelab_monitor.cli.cron.get_engine"),
        patch("homelab_monitor.cli.cron.SqliteRepository"),
        patch("homelab_monitor.cli.cron.CronRepo"),
    ):
        mock_discoverer = AsyncMock()
        mock_discoverer.scan = AsyncMock(return_value=mock_result)
        mock_discoverer_class.return_value = mock_discoverer

        rc = await _cmd_discover()
        assert rc == 1

        captured = capsys.readouterr()
        assert "discovered:" in captured.out
        assert "/etc/cron.d/broken" in captured.err
        assert "Parse error at line 1" in captured.err
