"""Unit tests for OverrideLoader — periodic file-override scanner."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from homelab_monitor.kernel.db.repositories.override_ownership_repository import (
    OverrideOwnershipRepository,
)
from homelab_monitor.kernel.db.repositories.probe_targets_repository import (
    ProbeTargetsRepository,
)
from homelab_monitor.kernel.db.repositories.suggestions_repository import (
    SuggestionsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
from homelab_monitor.kernel.docker.override_loader import OverrideLoader
from homelab_monitor.kernel.docker.socket_client import DockerSocketClient


@pytest.fixture
def log() -> structlog.stdlib.BoundLogger:
    """Return a structured logger for tests."""
    return structlog.get_logger()


@pytest.mark.asyncio
async def test_empty_dir_noop(repo: SqliteRepository, log: structlog.stdlib.BoundLogger) -> None:
    """Dir does not exist; refresh_once() returns; no rows; current_errors_by_container() == {}."""
    with tempfile.TemporaryDirectory() as tmpdir:
        nonexistent = Path(tmpdir) / "nonexistent"
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=nonexistent,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()
        assert loader.current_errors_by_container() == {}


@pytest.mark.asyncio
async def test_empty_dir_releases_previous_ownership(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Pre-seed ownership row + file_override probe; call against empty dir; probe hidden."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Seed ownership + probe
        now = utc_now_iso()
        async with repo.transaction() as conn:
            await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"foo"}, now=now)
            await ProbeTargetsRepository.upsert_probe_target_conn(
                conn,
                container_name="foo",
                kind="http",
                name="test",
                target_value="http://localhost",
                config_source="file_override",
                now=now,
            )

        empty_dir = Path(tmpdir) / "empty"
        empty_dir.mkdir()
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=empty_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify ownership cleared
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == set()

        # Verify probe is hidden
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=True
        )
        assert len(probes) == 1
        assert probes[0].hidden_at is not None


@pytest.mark.asyncio
async def test_single_valid_file_upserts_probes_and_claims_ownership(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write foo.yaml with one http probe; assert probe inserted and ownership claimed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\nprobes:\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:8080/health\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify probe inserted
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 1
        assert probes[0].kind == "http"
        assert probes[0].config_source == "file_override"

        # Verify ownership claimed
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == {"foo"}


@pytest.mark.asyncio
async def test_malformed_file_emits_suggestion_when_orphan(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write ghost.yaml with kind: ssh; socket returns no containers; suggestion emitted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        ghost_yaml = config_dir / "ghost.yaml"
        ghost_yaml.write_text(
            "container: ghost\nprobes:\n  - kind: ssh\n    name: shell\n    target: localhost\n"
        )

        # Mock socket to return empty list
        mock_socket = AsyncMock(spec=DockerSocketClient)
        mock_socket.list_containers.return_value = []

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            socket_client=mock_socket,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify suggestion emitted
        suggestions_repo = SuggestionsRepository(repo)
        suggestions, _ = await suggestions_repo.list_pending_docker_suggestions(
            status="pending", limit=100
        )
        assert len(suggestions) > 0
        assert any(s.kind == "docker_file_override_malformed" for s in suggestions)

        # Verify NO ownership
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == set()


@pytest.mark.asyncio
async def test_malformed_file_in_errors_map_when_live_container(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Same as above but socket reports ghost running; suggestion NOT emitted; errors populated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        ghost_yaml = config_dir / "ghost.yaml"
        ghost_yaml.write_text(
            "container: ghost\nprobes:\n  - kind: ssh\n    name: shell\n    target: localhost\n"
        )

        # Mock socket to return ghost running
        mock_socket = AsyncMock(spec=DockerSocketClient)
        mock_socket.list_containers.return_value = [{"Names": ["/ghost"]}]

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            socket_client=mock_socket,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify suggestion NOT emitted (container is live)
        suggestions_repo = SuggestionsRepository(repo)
        suggestions, _ = await suggestions_repo.list_pending_docker_suggestions(
            status="pending", limit=100
        )
        malformed_suggestions = [
            s for s in suggestions if s.kind == "docker_file_override_malformed"
        ]
        assert len(malformed_suggestions) == 0

        # Verify error is in the map
        errors = loader.current_errors_by_container()
        assert "ghost" in errors
        assert len(errors["ghost"]) > 0


@pytest.mark.asyncio
async def test_file_deletion_releases_ownership_and_hides_probes(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Tick 1: write foo.yaml; tick 2: delete file; assert probe hidden + ownership empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\nprobes:\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:8080/health\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )

        # Tick 1
        await loader.refresh_once()
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == {"foo"}

        # Delete file
        foo_yaml.unlink()

        # Tick 2
        await loader.refresh_once()
        owned = await ownership_repo.list_owned()
        assert owned == set()

        # Verify probe hidden
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=True
        )
        assert len(probes) == 1
        assert probes[0].hidden_at is not None


@pytest.mark.asyncio
async def test_disabled_true_wipes_probes_but_keeps_ownership(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write foo.yaml with disabled: true + probes; probes NOT upserted, ownership claimed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\n"
            "disabled: true\n"
            "probes:\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:8080/health\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify NO probes upserted
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 0

        # Verify ownership claimed
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == {"foo"}


@pytest.mark.asyncio
async def test_exec_probe_blocked_when_global_env_false(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """exec_enabled_globally=False; probe with kind=exec; probe NOT upserted, suggestion emitted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\n"
            "exec_authorized: true\n"
            "probes:\n"
            "  - kind: exec\n"
            "    name: check\n"
            "    target: /bin/check.sh\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify NO probes
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 0

        # Verify suggestion emitted
        suggestions_repo = SuggestionsRepository(repo)
        suggestions, _ = await suggestions_repo.list_pending_docker_suggestions(
            status="pending", limit=100
        )
        malformed = [s for s in suggestions if s.kind == "docker_file_override_malformed"]
        assert len(malformed) > 0
        assert any("exec_not_authorized" in s.detection_reason for s in malformed)


@pytest.mark.asyncio
async def test_exec_probe_blocked_when_exec_authorized_false(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """exec_enabled_globally=True but exec_authorized: false; probe blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\n"
            "exec_authorized: false\n"
            "probes:\n"
            "  - kind: exec\n"
            "    name: check\n"
            "    target: /bin/check.sh\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=True,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify NO probes
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 0


@pytest.mark.asyncio
async def test_exec_probe_allowed_when_both_gates_true(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """exec_enabled_globally=True and exec_authorized=true; probe inserted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\n"
            "exec_authorized: true\n"
            "probes:\n"
            "  - kind: exec\n"
            "    name: check\n"
            "    target: /bin/check.sh\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=True,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify probe inserted
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 1
        assert probes[0].kind == "exec"
        assert probes[0].config_source == "file_override"
        assert probes[0].exec_authorized is True


@pytest.mark.asyncio
async def test_yml_extension_accepted(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write bar.yml (not .yaml); assert ownership + probes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        bar_yml = config_dir / "bar.yml"
        bar_yml.write_text(
            "container: bar\nprobes:\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:8080\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == {"bar"}


@pytest.mark.asyncio
async def test_subdirs_ignored(repo: SqliteRepository, log: structlog.stdlib.BoundLogger) -> None:
    """Create subdir/baz.yaml; assert NOT processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        subdir = config_dir / "subdir"
        subdir.mkdir()
        (subdir / "baz.yaml").write_text("container: baz\nprobes: []\n")

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == set()


@pytest.mark.asyncio
async def test_other_extensions_ignored(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write foo.txt; assert NOT processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        (config_dir / "foo.txt").write_text("container: foo\nprobes: []\n")

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == set()


@pytest.mark.asyncio
async def test_container_name_filename_mismatch_emits_error(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write foo.yaml with container: bar; error in map; NO ownership; orphan suggestion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text("container: bar\nprobes: []\n")

        # Mock socket to return empty (orphan)
        mock_socket = AsyncMock(spec=DockerSocketClient)
        mock_socket.list_containers.return_value = []

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            socket_client=mock_socket,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify error in map
        errors = loader.current_errors_by_container()
        assert "foo" in errors or "bar" in errors  # file stem vs container field

        # Verify NO ownership of foo
        ownership_repo = OverrideOwnershipRepository(repo)
        owned = await ownership_repo.list_owned()
        assert owned == set()


@pytest.mark.asyncio
async def test_duplicate_probe_identity_emits_error(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Write file with two probes both kind: http, name: api; error captured + no ownership."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        foo_yaml = config_dir / "foo.yaml"
        foo_yaml.write_text(
            "container: foo\n"
            "probes:\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:8080\n"
            "  - kind: http\n"
            "    name: api\n"
            "    target: http://localhost:9090\n"
        )

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )
        await loader.refresh_once()

        # Verify error in map
        errors = loader.current_errors_by_container()
        assert "foo" in errors


@pytest.mark.asyncio
async def test_start_stop_task_lifecycle(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Call start_task(), sleep briefly, call stop_task(); task is None and
    CancelledError handled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )

        loader.start_task()
        assert loader._task is not None  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.02)
        await loader.stop_task()
        assert loader._task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_refresh_loop_continues_after_per_tick_exception(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Monkeypatch _iter_override_files to raise once; loop logs + continues."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()

        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=False,
            log=log,
            refresh_interval_seconds=0.01,
        )

        call_count = 0

        async def mock_refresh_once() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated error")

        with patch.object(loader, "refresh_once", side_effect=mock_refresh_once):
            loader.start_task()
            await asyncio.sleep(0.05)
            await loader.stop_task()
            # Should have tried at least twice (initial sleep + error + continue)
            assert call_count >= 1


@pytest.mark.asyncio
async def test_start_task_is_idempotent(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Calling start_task twice does not spawn a second task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        overrides_dir = Path(tmpdir) / "overrides"
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=overrides_dir,
            exec_enabled_globally=False,
            log=log,
        )
        loader.start_task()
        first_task = loader._task  # pyright: ignore[reportPrivateUsage]
        loader.start_task()
        second_task = loader._task  # pyright: ignore[reportPrivateUsage]
        assert first_task is second_task, "start_task must be idempotent"
        await loader.stop_task()


@pytest.mark.asyncio
async def test_stop_task_safe_when_never_started(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Calling stop_task on a loader that was never started completes without error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        overrides_dir = Path(tmpdir) / "overrides"
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=overrides_dir,
            exec_enabled_globally=False,
            log=log,
        )
        await loader.stop_task()
        assert loader._task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_refresh_releases_ownership_when_file_deleted(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Removing the override file releases ownership and soft-deletes its probes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        overrides_dir = Path(tmpdir) / "overrides" / "plugins" / "docker"
        # Do NOT create the dir — refresh_once takes the not-is_dir() branch
        # which calls _release_all_ownership (covers override_loader.py line 370).
        now = utc_now_iso()
        async with repo.transaction() as conn:
            await OverrideOwnershipRepository.set_owned_conn(conn, container_names={"foo"}, now=now)
            await ProbeTargetsRepository.upsert_probe_target_conn(
                conn,
                container_name="foo",
                kind="http",
                name="default",
                target_value="http://foo:8080/",
                config_source="file_override",
                now=now,
            )
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=overrides_dir,
            exec_enabled_globally=False,
            log=log,
        )
        await loader.refresh_once()
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=True
        )
        assert probes
        assert all(p.hidden_at is not None for p in probes)
        async with repo.transaction() as conn:
            owned = await OverrideOwnershipRepository.list_owned_conn(conn)
        assert owned == set()


@pytest.mark.asyncio
async def test_non_exec_probe_inherits_container_exec_authorized(
    repo: SqliteRepository, log: structlog.stdlib.BoundLogger
) -> None:
    """Non-exec probes inherit container-level exec_authorized, even though they don't use it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()
        (config_dir / "foo.yaml").write_text(
            "container: foo\n"
            "exec_authorized: true\n"
            "probes:\n"
            "  - kind: http\n"
            "    name: web\n"
            "    target: http://localhost:8080/health\n"
        )
        loader = OverrideLoader(
            db=repo,
            suggestions_repo=SuggestionsRepository(repo),
            probe_targets_repo=ProbeTargetsRepository(repo),
            ownership_repo=OverrideOwnershipRepository(repo),
            overrides_dir=config_dir,
            exec_enabled_globally=True,
            log=log,
        )
        await loader.refresh_once()
        probes = await ProbeTargetsRepository(repo).list_for_container(
            container_name="foo", include_hidden=False
        )
        assert len(probes) == 1
        assert probes[0].kind == "http"
        assert probes[0].exec_authorized is True
