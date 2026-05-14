"""Tests for CronDiscoverer (STAGE-002-007)."""

import socket
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.plugins.discoverers.cron_discoverer import (
    CronDiscoverer,
    _resolve_hostname,  # pyright: ignore[reportPrivateUsage]
    _resolve_interval,  # pyright: ignore[reportPrivateUsage]
)


def _make_host_tree(
    root: Path,
    *,
    crontab: str | None = None,
    cron_d_files: dict[str, str] | None = None,
    user_crontabs: dict[str, str] | None = None,
) -> None:
    """Build a fake /host tree structure."""
    (root / "etc").mkdir(parents=True, exist_ok=True)
    if crontab is not None:
        (root / "etc" / "crontab").write_text(crontab)
    if cron_d_files:
        (root / "etc" / "cron.d").mkdir(parents=True, exist_ok=True)
        for name, content in cron_d_files.items():
            (root / "etc" / "cron.d" / name).write_text(content)
    if user_crontabs:
        (root / "var" / "spool" / "cron" / "crontabs").mkdir(parents=True, exist_ok=True)
        for user, content in user_crontabs.items():
            (root / "var" / "spool" / "cron" / "crontabs" / user).write_text(content)


class _NullLog:
    """Minimal log object for tests."""

    def warning(self, *a: object, **kw: object) -> None:
        pass

    def info(self, *a: object, **kw: object) -> None:
        pass


@pytest.mark.asyncio
async def test_first_scan_inserts_rows_and_audits(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that first scan inserts a row and writes audit."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    discoverer = CronDiscoverer()
    cron_repo = CronRepo(repo)
    result = await discoverer.scan(cron_repo, log=_NullLog())

    assert result.inserted_count == 1
    assert result.partial is False
    assert len(result.found_fingerprints) == 1
    audit_row = await repo.fetch_one(
        text('SELECT what FROM audit_log WHERE what = :w ORDER BY "when" DESC LIMIT 1'),
        {"w": "crons.discover"},
    )
    assert audit_row is not None


@pytest.mark.asyncio
async def test_second_scan_bump_only_no_audit(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that second scan with same cron emits no audit for bump-only."""
    _make_host_tree(
        tmp_path,
        cron_d_files={
            "backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()
    await discoverer.scan(cron_repo, log=_NullLog())
    row = await repo.fetch_one(
        text("SELECT COUNT(*) AS c FROM audit_log WHERE what LIKE 'crons.discover%'")
    )
    assert row is not None
    audit_count_after_first = row.c

    # second scan — same fingerprint, no field change
    result = await discoverer.scan(cron_repo, log=_NullLog())
    assert result.inserted_count == 0
    assert result.bump_only_count == 1
    row = await repo.fetch_one(
        text("SELECT COUNT(*) AS c FROM audit_log WHERE what LIKE 'crons.discover%'")
    )
    assert row is not None
    audit_count_after_second = row.c
    assert audit_count_after_first == audit_count_after_second  # no new audit


@pytest.mark.asyncio
async def test_invalid_line_sets_partial(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that invalid schedule line sets partial=True."""
    _make_host_tree(
        tmp_path,
        cron_d_files={
            "bad": "*/X * * * * root /bin/false\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.partial is True
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_user_crontab_uses_filename_as_user(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that user crontab's source_path is crontab:user."""
    _make_host_tree(
        tmp_path,
        user_crontabs={
            "alice": "*/5 * * * * /opt/alice/sync.sh\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.inserted_count == 1
    # verify the source_path stored is "crontab:alice"
    row = await repo.fetch_one(text("SELECT source_path FROM crons LIMIT 1"))
    assert row is not None
    assert row.source_path == "crontab:alice"


@pytest.mark.asyncio
async def test_reboot_cron_cadence_zero(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that @reboot crons have cadence_seconds=0."""
    _make_host_tree(
        tmp_path,
        user_crontabs={
            "root": "@reboot /opt/init.sh\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    row = await repo.fetch_one(text("SELECT schedule, cadence_seconds FROM crons LIMIT 1"))
    assert row is not None
    assert row.schedule == "@reboot"
    assert row.cadence_seconds == 0


@pytest.mark.asyncio
async def test_missing_host_root_partial_with_errors(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that missing host root doesn't cause errors (files are optional)."""
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    # Missing optional files are not errors per discoverer design; result is empty + partial=False
    assert result.inserted_count == 0
    assert result.partial is False


@pytest.mark.asyncio
async def test_dotfiles_in_cron_d_skipped(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that dotfiles in cron.d are skipped."""
    _make_host_tree(
        tmp_path,
        cron_d_files={
            ".placeholder": "10 4 * * * root /bin/true\n",
            "real": "10 4 * * * root /bin/true\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    # Only "real" gets parsed — .placeholder skipped
    assert result.inserted_count == 1


# ---------------------------------------------------------------------------
# _resolve_interval() — error path (lines 47-48)
# ---------------------------------------------------------------------------


def test_resolve_interval_invalid_env_returns_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid HM_CRON_DISCOVERY_INTERVAL_SECONDS value returns default 300."""
    monkeypatch.setenv("HM_CRON_DISCOVERY_INTERVAL_SECONDS", "not-a-number")
    assert _resolve_interval() == 300  # noqa: PLR2004


# ---------------------------------------------------------------------------
# _resolve_hostname() — explicit env var and fallback paths (lines 60-68)
# ---------------------------------------------------------------------------


def test_resolve_hostname_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """HM_HOST_HOSTNAME set → returns that value without calling gethostname."""
    monkeypatch.setenv("HM_HOST_HOSTNAME", "my-explicit-host")
    result = _resolve_hostname(_NullLog())
    assert result == "my-explicit-host"


def test_resolve_hostname_fallback_to_gethostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """HM_HOST_HOSTNAME unset → falls back to socket.gethostname() with warning."""
    monkeypatch.delenv("HM_HOST_HOSTNAME", raising=False)
    expected = socket.gethostname()
    log = _NullLog()
    result = _resolve_hostname(log)
    assert result == expected


# ---------------------------------------------------------------------------
# run() with cron_repo=None (lines 86-96)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_without_cron_repo_returns_error() -> None:
    """CronDiscoverer.run() with cron_repo=None returns CollectorResult ok=False."""
    discoverer = CronDiscoverer()
    # cron_repo is None by default
    ctx = MagicMock()
    ctx.log = _NullLog()
    result = await discoverer.run(ctx)
    assert result.ok is False
    assert any("cron_repo not wired" in e for e in result.errors)


# ---------------------------------------------------------------------------
# scan() OSError on cron.d directory iteration (lines 139, 150)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_cron_d_iterdir_oserror_sets_partial(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError from cron.d iterdir captured as error and partial=True."""
    _make_host_tree(tmp_path)
    cron_d = tmp_path / "etc" / "cron.d"
    cron_d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    original_iterdir = Path.iterdir

    def patched_iterdir(self: Path) -> Iterator[Path]:  # type: ignore[override]
        if self == cron_d:
            raise OSError("permission denied cron.d")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", patched_iterdir)
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.partial is True
    assert any("permission denied cron.d" in e.error for e in result.errors)


# ---------------------------------------------------------------------------
# scan() OSError on spool directory iteration (lines 160, 162, 174-178)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_spool_iterdir_oserror_sets_partial(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError from spool/crontabs iterdir captured as error and partial=True."""
    spool = tmp_path / "var" / "spool" / "cron" / "crontabs"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    original_iterdir = Path.iterdir

    def patched_iterdir(self: Path) -> Iterator[Path]:  # type: ignore[override]
        if self == spool:
            raise OSError("permission denied spool")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", patched_iterdir)
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.partial is True
    assert any("permission denied spool" in e.error for e in result.errors)


# ---------------------------------------------------------------------------
# upsert exception handler (lines 174-178 in scan loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_upsert_exception_sets_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upsert_discovered raising an exception sets partial=True with error captured."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"test": "10 4 * * * root /bin/true\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    bad_repo = MagicMock()
    bad_repo.upsert_discovered = AsyncMock(side_effect=RuntimeError("db exploded"))

    result = await CronDiscoverer().scan(bad_repo, log=_NullLog())
    assert result.partial is True
    assert any("upsert failed" in e.error for e in result.errors)


# ---------------------------------------------------------------------------
# _scan_one_file() — file missing (line 198 early return)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_one_file_missing_returns_empty(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scan() with only the etc dir (no crontab file) triggers the missing-file early return."""
    # Create etc/ dir but NOT /etc/crontab, so _scan_one_file hits `return [], [], False`
    (tmp_path / "etc").mkdir(parents=True)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.partial is False
    assert result.inserted_count == 0


# ---------------------------------------------------------------------------
# _scan_one_file() — read_text raises PermissionError (lines 201-207)
# ---------------------------------------------------------------------------


def test_scan_one_file_permission_error_returns_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_scan_one_file with PermissionError on read_text returns partial=True."""
    from homelab_monitor.kernel.cron.discovery_types import CronSourceKind  # noqa: PLC0415

    target = tmp_path / "crontab"
    target.write_text("# placeholder")

    def raise_permission(self: Path, **kwargs: object) -> str:  # type: ignore[override]
        raise PermissionError("no read permission")

    monkeypatch.setattr(Path, "read_text", raise_permission)

    discoverer = CronDiscoverer()
    entries, errors, partial = discoverer._scan_one_file(  # pyright: ignore[reportPrivateUsage]
        container_path=target,
        host_source_path="/etc/crontab",
        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
        host="h",
    )
    assert entries == []
    assert len(errors) == 1
    assert partial is True


# ---------------------------------------------------------------------------
# _scan_one_file() — one valid + one parse-error line (lines 248-249)
# ---------------------------------------------------------------------------


def test_scan_one_file_mixed_valid_and_error(tmp_path: Path) -> None:
    """_scan_one_file with one valid + one bad line returns partial=True."""
    from homelab_monitor.kernel.cron.discovery_types import CronSourceKind  # noqa: PLC0415

    target = tmp_path / "crontab"
    target.write_text("10 4 * * * root /bin/true\n*/X * * * * root /bin/false\n")

    discoverer = CronDiscoverer()
    entries, errors, partial = discoverer._scan_one_file(  # pyright: ignore[reportPrivateUsage]
        container_path=target,
        host_source_path="/etc/cron.d/mixed",
        source_kind=CronSourceKind.SYSTEM_WITH_USER_FIELD,
        host="h",
    )
    assert len(entries) == 1
    assert len(errors) == 1
    assert partial is True


# ---------------------------------------------------------------------------
# _resolve_hostname() — log object without warning attr (branch 62->68)
# ---------------------------------------------------------------------------


def test_resolve_hostname_fallback_no_warning_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    """When log has no 'warning' attr, fallback still returns gethostname()."""
    monkeypatch.delenv("HM_HOST_HOSTNAME", raising=False)
    # pass an object with no warning method
    result = _resolve_hostname(object())
    assert result == socket.gethostname()


# ---------------------------------------------------------------------------
# run() success path (lines 95-96) + log.info branch (lines 209-219)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_cron_repo_returns_ok(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() with cron_repo wired executes scan and returns CollectorResult ok=True."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    discoverer = CronDiscoverer()
    discoverer.cron_repo = CronRepo(repo)
    ctx = MagicMock()
    ctx.log = _NullLog()
    result = await discoverer.run(ctx)
    assert result.ok is True
    assert result.metrics_emitted == 0


# ---------------------------------------------------------------------------
# spool: dotfile skip (line 160) and non-file (directory) skip (line 162)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_spool_dotfile_skipped(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dotfiles in spool/crontabs are skipped; only real user files parsed."""
    _make_host_tree(
        tmp_path,
        user_crontabs={
            "alice": "*/5 * * * * /opt/alice/sync.sh\n",
        },
    )
    # Add a dotfile to the spool directory
    dotfile = tmp_path / "var" / "spool" / "cron" / "crontabs" / ".hidden"
    dotfile.write_text("* * * * * /bin/hidden\n")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.inserted_count == 1  # only alice, not .hidden


@pytest.mark.asyncio
async def test_scan_spool_subdirectory_skipped(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subdirectories in spool/crontabs are skipped (not files)."""
    _make_host_tree(
        tmp_path,
        user_crontabs={
            "alice": "*/5 * * * * /opt/alice/sync.sh\n",
        },
    )
    # Add a subdirectory to spool — should be skipped (is_file() check)
    subdir = tmp_path / "var" / "spool" / "cron" / "crontabs" / "subdir"
    subdir.mkdir()
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.inserted_count == 1  # only alice, not subdir


@pytest.mark.asyncio
async def test_scan_cron_d_subdirectory_skipped(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subdirectories in cron.d are skipped (is_file() check, line 139)."""
    _make_host_tree(
        tmp_path,
        cron_d_files={
            "real": "10 4 * * * root /bin/true\n",
        },
    )
    # Add a subdirectory to cron.d — should be skipped
    subdir = tmp_path / "etc" / "cron.d" / "subdir"
    subdir.mkdir()
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.inserted_count == 1  # only real, not subdir


@pytest.mark.asyncio
async def test_scan_updated_non_bump_increments_updated_count(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When upsert returns updated_non_bump=True, scan increments updated_count (line 198)."""
    import homelab_monitor.kernel.cron.schedule as schedule_mod  # noqa: PLC0415

    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    # First scan — inserts
    await discoverer.scan(cron_repo, log=_NullLog())

    # Patch canonicalize_schedule to return a different value so second scan triggers update
    monkeypatch.setattr(schedule_mod, "canonicalize_schedule", lambda s: "0 4 * * *")  # type: ignore[reportUnknownLambdaType]
    result = await discoverer.scan(cron_repo, log=_NullLog())
    assert result.updated_count == 1
    assert result.inserted_count == 0


class _NoInfoLog:
    """Log object without info method (exercises hasattr(log, 'info') False branch)."""

    def warning(self, *a: object, **kw: object) -> None:
        pass


@pytest.mark.asyncio
async def test_scan_log_without_info_attr(
    repo: SqliteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scan() with a log object missing 'info' attr skips the log.info call (line 209->219)."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /bin/true\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    result = await CronDiscoverer().scan(CronRepo(repo), log=_NoInfoLog())
    assert result.inserted_count == 1


class _WarningCapturingLog:
    """Log object that captures warning calls."""

    def __init__(self) -> None:
        self.warning_count = 0

    def warning(self, *a: object, **kw: object) -> None:
        self.warning_count += 1

    def info(self, *a: object, **kw: object) -> None:
        pass


def test_hostname_fallback_warning_fires_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_hostname logs warning only on first call when env var unset."""
    import homelab_monitor.plugins.discoverers.cron_discoverer as discoverer_mod  # noqa: PLC0415

    # Reset the module-level flag
    monkeypatch.setattr(discoverer_mod, "_hostname_fallback_warned", False)
    # Ensure env var is unset
    monkeypatch.delenv("HM_HOST_HOSTNAME", raising=False)

    log = _WarningCapturingLog()

    # First call should emit warning
    hostname1 = _resolve_hostname(log)
    assert log.warning_count == 1
    assert hostname1 == socket.gethostname()

    # Second call should NOT emit warning (flag is set)
    hostname2 = _resolve_hostname(log)
    assert log.warning_count == 1  # still 1, not 2
    assert hostname2 == socket.gethostname()
