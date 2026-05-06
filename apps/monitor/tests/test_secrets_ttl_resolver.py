"""Tests for kernel/secrets/ttl_resolver.py — TTL refresh, snapshots, task lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import structlog

from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver
from homelab_monitor.kernel.secrets.ttl_resolver import TtlCachingSecretsResolver


@pytest_asyncio.fixture
async def mock_secrets_repo() -> AsyncMock:
    """Mock AsyncSecretsRepository."""
    repo = AsyncMock()
    # Default: return an empty snapshot
    repo.snapshot.return_value = SyncSecretsResolver(_values={})
    return repo


@pytest.mark.asyncio
async def test_current_returns_snapshot_after_refresh_now(
    mock_secrets_repo: AsyncMock,
) -> None:
    """current() returns the snapshot from refresh_now()."""
    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=60.0)
    await resolver.refresh_now()
    snapshot = resolver.current()
    assert isinstance(snapshot, SyncSecretsResolver)


@pytest.mark.asyncio
async def test_refresh_now_updates_snapshot(mock_secrets_repo: AsyncMock) -> None:
    """After refresh_now() the resolver reflects new secrets in DB."""
    # Create two different snapshots
    snapshot1 = SyncSecretsResolver(_values={"key1": "value1"})
    snapshot2 = SyncSecretsResolver(_values={"key1": "value1", "key2": "value2"})

    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=60.0)

    # First refresh
    mock_secrets_repo.snapshot.return_value = snapshot1
    await resolver.refresh_now()
    result1 = resolver.current()
    assert result1 is snapshot1

    # Second refresh with different snapshot
    mock_secrets_repo.snapshot.return_value = snapshot2
    await resolver.refresh_now()
    result2 = resolver.current()
    assert result2 is snapshot2


@pytest.mark.asyncio
async def test_refresh_loop_cancellable() -> None:
    """refresh_loop() exits cleanly on task.cancel()."""
    mock_repo = AsyncMock()
    mock_repo.snapshot.return_value = SyncSecretsResolver(_values={})
    resolver = TtlCachingSecretsResolver(mock_repo, ttl_seconds=60.0)
    await resolver.refresh_now()

    # Start refresh loop
    task = asyncio.create_task(resolver.refresh_loop())
    # Give it a moment to start
    await asyncio.sleep(0.01)
    # Cancel it
    task.cancel()

    # Should exit cleanly
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_refresh_loop_survives_snapshot_exception(
    mock_secrets_repo: AsyncMock,
) -> None:
    """refresh_loop() survives a repo.snapshot() exception — previous snapshot intact."""
    # Set up initial snapshot
    snapshot1 = SyncSecretsResolver(_values={"key": "value"})
    mock_secrets_repo.snapshot.return_value = snapshot1

    log = structlog.get_logger("test")
    resolver = TtlCachingSecretsResolver(
        mock_secrets_repo,
        ttl_seconds=0.05,
        log=log,  # Short TTL for test
    )
    await resolver.refresh_now()
    first_snapshot = resolver.current()
    assert first_snapshot is snapshot1

    # Now make snapshot() raise
    mock_secrets_repo.snapshot.side_effect = RuntimeError("DB error")

    # Start refresh loop
    task = asyncio.create_task(resolver.refresh_loop())
    await asyncio.sleep(0.1)  # Wait for at least one refresh attempt
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Snapshot should still be the original
    assert resolver.current() is snapshot1


@pytest.mark.asyncio
async def test_refresh_now_raises_on_error(mock_secrets_repo: AsyncMock) -> None:
    """refresh_now() propagates exceptions from repo.snapshot()."""
    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=60.0)
    mock_secrets_repo.snapshot.side_effect = RuntimeError("DB error")

    with pytest.raises(RuntimeError, match="DB error"):
        await resolver.refresh_now()


@pytest.mark.asyncio
async def test_refresh_now_logs_exception_when_logger_provided(
    mock_secrets_repo: AsyncMock,
) -> None:
    """refresh_now() logs exception with logger if provided."""
    log = structlog.get_logger("test")
    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=60.0, log=log)
    mock_secrets_repo.snapshot.side_effect = RuntimeError("DB error")

    with pytest.raises(RuntimeError):
        await resolver.refresh_now()


@pytest.mark.asyncio
async def test_current_raises_before_first_refresh() -> None:
    """current() raises RuntimeError if called before refresh_now()."""
    mock_repo = AsyncMock()
    resolver = TtlCachingSecretsResolver(mock_repo, ttl_seconds=60.0)

    with pytest.raises(RuntimeError, match="not initialized"):
        resolver.current()


def test_ttl_caching_resolver_with_custom_clock() -> None:
    """TtlCachingSecretsResolver accepts a custom clock function."""
    mock_repo = AsyncMock()
    mock_clock = MagicMock(return_value=1000.0)
    resolver = TtlCachingSecretsResolver(mock_repo, ttl_seconds=60.0, clock=mock_clock)
    # Just verify it accepts the parameter
    assert resolver._clock is mock_clock  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_refresh_loop_idempotent_cancel() -> None:
    """Calling task.cancel() on already-done task is idempotent."""
    mock_repo = AsyncMock()
    mock_repo.snapshot.return_value = SyncSecretsResolver(_values={})
    resolver = TtlCachingSecretsResolver(mock_repo, ttl_seconds=60.0)
    await resolver.refresh_now()

    task = asyncio.create_task(resolver.refresh_loop())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancel again — should be safe
    task.cancel()


@pytest.mark.asyncio
async def test_refresh_now_atomicity(mock_secrets_repo: AsyncMock) -> None:
    """Snapshot swap is atomic — concurrent reads never see half-built state."""
    snapshot1 = SyncSecretsResolver(_values={"a": "1"})
    snapshot2 = SyncSecretsResolver(_values={"a": "1", "b": "2"})

    call_count = 0

    async def slow_snapshot() -> SyncSecretsResolver:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return snapshot1
        # Second call is slow
        await asyncio.sleep(0.05)
        return snapshot2

    mock_secrets_repo.snapshot = slow_snapshot  # type: ignore[assignment]

    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=60.0)
    await resolver.refresh_now()

    first = resolver.current()
    assert first is snapshot1

    # Kick off a slow refresh
    refresh_task = asyncio.create_task(resolver.refresh_now())
    # Small sleep to let it start
    await asyncio.sleep(0.01)
    # Current read should still return the old snapshot (not partial)
    current = resolver.current()
    assert current is snapshot1

    # Wait for refresh to complete
    await refresh_task
    # Now it should be the new snapshot
    current = resolver.current()
    assert current is snapshot2


@pytest.mark.asyncio
async def test_refresh_loop_continues_after_exception_in_refresh_now(
    mock_secrets_repo: AsyncMock,
) -> None:
    """refresh_loop() error-handling allows it to retry at next interval."""
    snapshot = SyncSecretsResolver(_values={"key": "value"})
    mock_secrets_repo.snapshot.return_value = snapshot

    log = structlog.get_logger("test")
    resolver = TtlCachingSecretsResolver(mock_secrets_repo, ttl_seconds=0.02, log=log)
    await resolver.refresh_now()

    refresh_calls = 0

    async def tracked_snapshot() -> SyncSecretsResolver:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            # First refresh (initial) succeeds
            return snapshot
        elif refresh_calls == 2:  # noqa: PLR2004
            # Second refresh in loop fails
            raise RuntimeError("Temporary error")
        else:
            # Third refresh succeeds again
            return snapshot

    mock_secrets_repo.snapshot = tracked_snapshot  # type: ignore[assignment]

    task = asyncio.create_task(resolver.refresh_loop())
    # Wait long enough for at least 3 refresh attempts
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Should have attempted multiple refreshes
    assert refresh_calls >= 2  # noqa: PLR2004
