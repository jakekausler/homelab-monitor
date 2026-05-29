"""Tests for RedactionAuditCollector (STAGE-004-006)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homelab_monitor.kernel.metrics.redaction_audit import RedactionAuditCollector


class TestRedactionAuditCollector:
    """Test the redaction audit collector."""

    @pytest.mark.asyncio
    async def test_writes_counts_only_audit_row(self) -> None:
        """Mock VM response with two pattern_type series; write ONE audit row."""
        collector = RedactionAuditCollector()

        # Mock context
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        # Mock VM instant response
        vm_response = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {
                            "pattern_type": "bearer_token",
                            "__name__": "vector_redactions_total",
                        },
                        "value": ["1715432100", "5"],
                    },
                    {
                        "metric": {
                            "pattern_type": "jwt",
                            "__name__": "vector_redactions_total",
                        },
                        "value": ["1715432100", "3"],
                    },
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = vm_response
        ctx.http.get.return_value = mock_resp

        # Mock audit_write
        with patch(
            "homelab_monitor.kernel.metrics.redaction_audit.audit_write",
            new_callable=AsyncMock,
        ) as mock_audit:
            result = await collector.run(ctx)

        # Check result
        assert result.ok
        assert result.metrics_emitted == 0

        # Check audit_write was called
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["who"] == "system"
        assert call_args.kwargs["what"] == "logs.redaction_counts"
        after = call_args.kwargs["after"]

        # Verify counts-only: after dict should have delta and cumulative per pattern_type
        assert "bearer_token" in after
        assert "jwt" in after
        assert after["bearer_token"]["delta"] == 5  # noqa: PLR2004
        assert after["bearer_token"]["cumulative"] == 5  # noqa: PLR2004
        assert after["jwt"]["delta"] == 3  # noqa: PLR2004
        assert after["jwt"]["cumulative"] == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_delta_across_two_ticks(self) -> None:
        """tick1: cumulative 5; tick2: cumulative 8 → delta==3."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        # Tick 1
        vm_response_1 = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pattern_type": "bearer_token"},
                        "value": ["1715432100", "5"],
                    },
                ]
            },
        }
        mock_resp_1 = MagicMock()
        mock_resp_1.status_code = 200
        mock_resp_1.json.return_value = vm_response_1
        ctx.http.get.return_value = mock_resp_1

        with patch(
            "homelab_monitor.kernel.metrics.redaction_audit.audit_write",
            new_callable=AsyncMock,
        ) as mock_audit:
            await collector.run(ctx)
            first_call = mock_audit.call_args.kwargs["after"]
            assert first_call["bearer_token"]["delta"] == 5  # noqa: PLR2004

            # Tick 2
            vm_response_2 = {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"pattern_type": "bearer_token"},
                            "value": ["1715432160", "8"],
                        },
                    ]
                },
            }
            mock_resp_2 = MagicMock()
            mock_resp_2.status_code = 200
            mock_resp_2.json.return_value = vm_response_2
            ctx.http.get.return_value = mock_resp_2

            await collector.run(ctx)
            second_call = mock_audit.call_args.kwargs["after"]
            assert second_call["bearer_token"]["delta"] == 3  # noqa: PLR2004
            assert second_call["bearer_token"]["cumulative"] == 8  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_no_audit_when_no_delta(self) -> None:
        """No new redactions → audit_write NOT called."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        # Tick 1: set last_seen
        vm_response_1 = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pattern_type": "bearer_token"},
                        "value": ["1715432100", "5"],
                    },
                ]
            },
        }
        mock_resp_1 = MagicMock()
        mock_resp_1.status_code = 200
        mock_resp_1.json.return_value = vm_response_1
        ctx.http.get.return_value = mock_resp_1

        with patch(
            "homelab_monitor.kernel.metrics.redaction_audit.audit_write",
            new_callable=AsyncMock,
        ) as mock_audit:
            await collector.run(ctx)
            assert mock_audit.call_count == 1

            # Tick 2: same cumulative
            vm_response_2 = {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"pattern_type": "bearer_token"},
                            "value": ["1715432160", "5"],
                        },
                    ]
                },
            }
            mock_resp_2 = MagicMock()
            mock_resp_2.status_code = 200
            mock_resp_2.json.return_value = vm_response_2
            ctx.http.get.return_value = mock_resp_2

            await collector.run(ctx)
            # Should still be 1 call (no new call on tick 2)
            assert mock_audit.call_count == 1

    @pytest.mark.asyncio
    async def test_counter_reset_rebaselines(self) -> None:
        """Cumulative < previous → no negative delta, re-baseline."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        # Tick 1: set to 10
        vm_response_1 = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pattern_type": "bearer_token"},
                        "value": ["1715432100", "10"],
                    },
                ]
            },
        }
        mock_resp_1 = MagicMock()
        mock_resp_1.status_code = 200
        mock_resp_1.json.return_value = vm_response_1
        ctx.http.get.return_value = mock_resp_1

        with patch(
            "homelab_monitor.kernel.metrics.redaction_audit.audit_write",
            new_callable=AsyncMock,
        ) as mock_audit:
            await collector.run(ctx)
            assert mock_audit.call_count == 1

            # Tick 2: reset to 5
            vm_response_2 = {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": {"pattern_type": "bearer_token"},
                            "value": ["1715432160", "5"],
                        },
                    ]
                },
            }
            mock_resp_2 = MagicMock()
            mock_resp_2.status_code = 200
            mock_resp_2.json.return_value = vm_response_2
            ctx.http.get.return_value = mock_resp_2

            await collector.run(ctx)
            # No new audit call (delta < 0 not emitted, re-baselined)
            assert mock_audit.call_count == 1

    @pytest.mark.asyncio
    async def test_vm_down_no_crash(self) -> None:
        """VM query raises → result ok=False, no exception."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()
        ctx.http.get.side_effect = RuntimeError("Connection refused")

        result = await collector.run(ctx)
        assert not result.ok
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_query_counts_non_200_raises_runtime_error(self) -> None:
        """VM returns status 500 → _query_counts raises RuntimeError."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        ctx.http.get.return_value = mock_resp

        with pytest.raises(RuntimeError):
            await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_query_counts_status_error_returns_empty(self) -> None:
        """VM json has status='error' → _query_counts returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "error"}
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_query_counts_non_dict_series_skipped(self) -> None:
        """Series item that is not a dict is skipped → returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {"result": ["not-a-dict"]},
        }
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_query_counts_non_list_value_skipped(self) -> None:
        """Series with value not a list is skipped → returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pattern_type": "bearer_token"}, "value": "notalist"},
                ]
            },
        }
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_query_counts_short_value_list_skipped(self) -> None:
        """Series with value list of length < 2 is skipped → returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pattern_type": "bearer_token"}, "value": ["only-one"]},
                ]
            },
        }
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_query_counts_non_str_pattern_type_skipped(self) -> None:
        """Series with non-str pattern_type is skipped → returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pattern_type": 123}, "value": ["ts", "5"]},
                ]
            },
        }
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_query_counts_non_numeric_value_skipped(self) -> None:
        """Series with non-numeric value[1] is skipped → returns {}."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pattern_type": "bearer_token"}, "value": ["ts", "not-a-number"]},
                ]
            },
        }
        ctx.http.get.return_value = mock_resp

        result = await collector._query_counts(ctx, "http://vm")  # pyright: ignore[reportPrivateUsage]
        assert result == {}

    @pytest.mark.asyncio
    async def test_never_writes_to_vl(self) -> None:
        """Collector has no reference to ctx.vl."""
        collector = RedactionAuditCollector()
        ctx = MagicMock()
        ctx.log = MagicMock()
        ctx.db = MagicMock()
        ctx.http = AsyncMock()

        vm_response = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"pattern_type": "bearer_token"},
                        "value": ["1715432100", "5"],
                    },
                ]
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = vm_response
        ctx.http.get.return_value = mock_resp

        with patch(
            "homelab_monitor.kernel.metrics.redaction_audit.audit_write",
            new_callable=AsyncMock,
        ):
            await collector.run(ctx)

        # ctx.vl should never be accessed
        assert (
            not hasattr(ctx, "vl")
            or ctx.vl.mock_calls == []
            or not any("vl" in str(c) for c in ctx.method_calls)
        )
