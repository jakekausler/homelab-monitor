"""Tests for CronDiscoverer (STAGE-002-007)."""

import socket
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.cron.log_match import canonical_log_key
from homelab_monitor.kernel.cron.repository import CronRepo
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.db.time import utc_now_iso
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
    """Build a fake /host tree plus a sibling crontab-snapshot dir.

    `user_crontabs` keys are usernames; values are raw `crontab -l` output.
    They are written into <root>/crontab-snapshot/<user> — the layout the
    hm-crontab-snapshot host script produces and the discoverer reads via
    HM_CRON_SNAPSHOT_DIR.
    """
    (root / "etc").mkdir(parents=True, exist_ok=True)
    if crontab is not None:
        (root / "etc" / "crontab").write_text(crontab)
    if cron_d_files:
        (root / "etc" / "cron.d").mkdir(parents=True, exist_ok=True)
        for name, content in cron_d_files.items():
            (root / "etc" / "cron.d" / name).write_text(content)
    if user_crontabs:
        snap = root / "crontab-snapshot"
        snap.mkdir(parents=True, exist_ok=True)
        for user, content in user_crontabs.items():
            (snap / user).write_text(content)


class _NullLog:
    """Minimal log object for tests."""

    def warning(self, *a: object, **kw: object) -> None:
        pass

    def info(self, *a: object, **kw: object) -> None:
        pass


def _patch_read_text_permission_error(monkeypatch: pytest.MonkeyPatch, *, deny_suffix: str) -> None:
    """Monkeypatch Path.read_text to raise PermissionError for any path whose
    string ends with `deny_suffix`, and behave normally for all others.

    Used to simulate a 0600 crontab the container UID cannot read WITHOUT
    relying on chmod 0o000 (the test runner may be root, for which chmod is
    a no-op).
    """
    original_read_text = Path.read_text

    def patched(self: Path, *args: object, **kwargs: object) -> str:
        if str(self).endswith(deny_suffix):
            raise PermissionError(f"simulated permission denied: {self}")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", patched)


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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    """OSError from snapshot dir iterdir captured as error and partial=True."""
    snapshot_dir = tmp_path / "crontab-snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    original_iterdir = Path.iterdir

    def patched_iterdir(self: Path) -> Iterator[Path]:  # type: ignore[override]
        if self == snapshot_dir:
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    # Add a dotfile to the snapshot directory — should be skipped
    dotfile = tmp_path / "crontab-snapshot" / ".hidden"
    dotfile.write_text("* * * * * /bin/hidden\n")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    # Add a subdirectory to the snapshot dir — should be skipped (is_file() check)
    subdir = tmp_path / "crontab-snapshot" / "subdir"
    subdir.mkdir()
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
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


# Wave 2 extensions: test new scan() result fields


@pytest.mark.asyncio
async def test_scan_populates_clean_source_paths(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that scan() populates clean_source_paths with successfully scanned paths."""
    _make_host_tree(
        tmp_path,
        crontab="0 * * * * echo system\n",
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    discoverer = CronDiscoverer()
    cron_repo = CronRepo(repo)

    result = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/crontab" in result.clean_source_paths
    assert "/etc/cron.d/backup" in result.clean_source_paths
    assert result.host == "test-host"


@pytest.mark.asyncio
async def test_scan_found_by_source_path_grouping(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that found_by_source_path groups fingerprints by source path."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    discoverer = CronDiscoverer()
    cron_repo = CronRepo(repo)

    result = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/cron.d/backup" in result.found_by_source_path
    found_fps = result.found_by_source_path["/etc/cron.d/backup"]
    assert len(found_fps) == 1
    assert found_fps == result.found_fingerprints


@pytest.mark.asyncio
async def test_scan_cron_d_iterdir_oserror_excludes_from_clean(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that iterdir failure on cron.d excludes it from clean_source_paths."""
    _make_host_tree(tmp_path, crontab="0 * * * * echo system\n")
    (tmp_path / "etc" / "cron.d").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    # Mock iterdir to fail on cron.d
    def mock_iterdir(self: Path) -> Iterator[Path]:
        if self.name == "cron.d":
            raise OSError("permission denied")
        return object.__getattribute__(self, "_real_iterdir")()

    original_iterdir = Path.iterdir
    Path._real_iterdir = original_iterdir  # type: ignore[attr-defined]
    monkeypatch.setattr(Path, "iterdir", mock_iterdir)

    discoverer = CronDiscoverer()
    cron_repo = CronRepo(repo)

    result = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/cron.d" in result.unreachable_source_path_prefixes
    assert not any(p.startswith("/etc/cron.d") for p in result.clean_source_paths)
    assert result.partial is True


@pytest.mark.asyncio
async def test_scan_parse_error_excludes_file_from_clean(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that a file with parse errors is excluded from clean_source_paths."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"bad": "*/X * * * * root /bin/false\n"},  # Invalid schedule
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    discoverer = CronDiscoverer()
    cron_repo = CronRepo(repo)

    result = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/cron.d/bad" not in result.clean_source_paths
    assert result.partial is True


@pytest.mark.asyncio
async def test_run_reconcile_soft_deletes_raises_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() continues and returns CollectorResult when reconcile_soft_deletes raises."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/cron/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    # Create a mock cron_repo whose reconcile_soft_deletes raises
    mock_repo = AsyncMock()
    mock_repo.reconcile_soft_deletes = AsyncMock(side_effect=RuntimeError("db error"))
    # scan() calls register_cron for each entry; mock it to return a plausible value
    from homelab_monitor.kernel.cron.repository import CronRecord  # noqa: PLC0415

    fake_record = MagicMock(spec=CronRecord)
    mock_repo.register_cron = AsyncMock(return_value=(fake_record, True))
    mock_repo.list_source_paths_for_host = AsyncMock(return_value=frozenset())

    discoverer = CronDiscoverer()
    discoverer.cron_repo = mock_repo
    ctx = MagicMock()
    ctx.log = _NullLog()

    result = await discoverer.run(ctx)
    assert result is not None
    assert result.metrics_emitted == 0


@pytest.mark.asyncio
async def test_run_reconcile_raises_log_without_warning_attr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() skips ctx.log.warning when reconcile raises and log lacks .warning (113->117)."""
    _make_host_tree(tmp_path)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    mock_repo = AsyncMock()
    mock_repo.reconcile_soft_deletes = AsyncMock(side_effect=RuntimeError("fail"))
    mock_repo.register_cron = AsyncMock(return_value=(MagicMock(), False))
    mock_repo.list_source_paths_for_host = AsyncMock(return_value=frozenset())

    class _NoWarningLog:
        """Log without .warning — triggers hasattr(ctx.log, 'warning') to be False."""

        def info(self, *a: object, **kw: object) -> None:
            pass

    discoverer = CronDiscoverer()
    discoverer.cron_repo = mock_repo
    ctx = MagicMock()
    ctx.log = _NoWarningLog()

    result = await discoverer.run(ctx)
    assert result is not None


@pytest.mark.asyncio
async def test_run_reconcile_ok_log_without_info_attr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() skips ctx.log.info call when log object lacks .info (117->123 branch)."""
    _make_host_tree(tmp_path)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    mock_repo = AsyncMock()
    mock_repo.reconcile_soft_deletes = AsyncMock(return_value=(0, 0))
    mock_repo.register_cron = AsyncMock(return_value=(MagicMock(), False))
    mock_repo.list_source_paths_for_host = AsyncMock(return_value=frozenset())

    class _NoInfoLog:
        """Log without .info — triggers hasattr check at line 117 to be False."""

        def warning(self, *a: object, **kw: object) -> None:
            pass

    discoverer = CronDiscoverer()
    discoverer.cron_repo = mock_repo
    ctx = MagicMock()
    ctx.log = _NoInfoLog()

    result = await discoverer.run(ctx)
    assert result is not None


@pytest.mark.asyncio
async def test_scan_crontab_errors_excluded_from_clean_source_paths(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse error in /etc/crontab keeps it out of clean_source_paths (162->166)."""
    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc" / "crontab").write_text("*/X * * * * root /bin/false\n")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())

    assert "/etc/crontab" not in result.clean_source_paths


@pytest.mark.asyncio
async def test_scan_spool_errors_excluded_from_clean_source_paths(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse error in a user crontab keeps it out of clean_source_paths (211->195)."""
    snap = tmp_path / "crontab-snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "alice").write_text("*/X * * * * /bin/false\n")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())

    assert "crontab:alice" not in result.clean_source_paths


@pytest.mark.asyncio
async def test_scan_known_db_path_not_owned_by_scanner_skipped(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB path outside /etc/cron.d/ or crontab: is not added to clean_source_paths.

    This covers the 285->281 branch (the if-condition at 285-290 is False).
    """
    _make_host_tree(tmp_path)
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    from homelab_monitor.kernel.cron import repository as _repo_mod  # noqa: PLC0415

    async def _fake_list(self: object, host: str) -> frozenset[str]:
        return frozenset(["/some/unknown/path"])

    monkeypatch.setattr(_repo_mod.CronRepo, "list_source_paths_for_host", _fake_list)

    cron_repo = CronRepo(repo)
    result = await CronDiscoverer().scan(cron_repo, log=_NullLog())

    assert "/some/unknown/path" not in result.clean_source_paths


# ---------------------------------------------------------------------------
# STAGE-002-007A bugfix: unreadable paths must NOT be soft-deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_user_crontab_not_soft_deleted(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STAGE-002-007A bugfix: a user crontab that becomes unreadable mid-life
    must NOT have its registered crons soft-deleted.

    Scan 1: file is readable -> cron is discovered + registered.
    Scan 2: file is unreadable (PermissionError) -> the cron's row must NOT
    be soft-deleted, and reconcile must report 0 soft-deleted / 0 restored
    for it.
    """
    _make_host_tree(
        tmp_path,
        user_crontabs={"jakekausler": "*/5 * * * * /opt/sync.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    # Scan 1 — readable: discovers the cron.
    result1 = await discoverer.scan(cron_repo, log=_NullLog())
    assert result1.inserted_count == 1
    assert "crontab:jakekausler" in result1.clean_source_paths
    (fp,) = tuple(result1.found_fingerprints)

    # Scan 2 — the file is now unreadable.
    _patch_read_text_permission_error(monkeypatch, deny_suffix="/crontab-snapshot/jakekausler")
    result2 = await discoverer.scan(cron_repo, log=_NullLog())

    # The unreadable path must be tracked and excluded from clean_source_paths.
    assert "crontab:jakekausler" in result2.unreadable_source_paths
    assert "crontab:jakekausler" not in result2.clean_source_paths
    assert result2.partial is True

    # Reconcile with the scan-2 result must NOT soft-delete the cron.
    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host=result2.host,
        clean_paths=result2.clean_source_paths,
        found_by_path=result2.found_by_source_path,
        now=utc_now_iso(),
    )
    assert soft_deleted == 0
    assert restored == 0

    # The row's soft_deleted_at must still be NULL.
    row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row.soft_deleted_at is None


@pytest.mark.asyncio
async def test_unreadable_cron_d_file_not_soft_deleted(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STAGE-002-007A bugfix: an /etc/cron.d/<file> that becomes unreadable
    must NOT have its registered crons soft-deleted."""
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    result1 = await discoverer.scan(cron_repo, log=_NullLog())
    assert result1.inserted_count == 1
    (fp,) = tuple(result1.found_fingerprints)

    _patch_read_text_permission_error(monkeypatch, deny_suffix="/cron.d/backup")
    result2 = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/cron.d/backup" in result2.unreadable_source_paths
    assert "/etc/cron.d/backup" not in result2.clean_source_paths
    assert result2.partial is True

    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host=result2.host,
        clean_paths=result2.clean_source_paths,
        found_by_path=result2.found_by_source_path,
        now=utc_now_iso(),
    )
    assert soft_deleted == 0
    assert restored == 0

    row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row.soft_deleted_at is None


@pytest.mark.asyncio
async def test_unreadable_etc_crontab_not_soft_deleted(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STAGE-002-007A bugfix: an unreadable /etc/crontab must NOT have its
    registered crons soft-deleted."""
    _make_host_tree(tmp_path, crontab="0 3 * * * root /storage/scripts/nightly.sh\n")
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    result1 = await discoverer.scan(cron_repo, log=_NullLog())
    assert result1.inserted_count == 1
    assert "/etc/crontab" in result1.clean_source_paths
    (fp,) = tuple(result1.found_fingerprints)

    _patch_read_text_permission_error(monkeypatch, deny_suffix="/etc/crontab")
    result2 = await discoverer.scan(cron_repo, log=_NullLog())

    assert "/etc/crontab" in result2.unreadable_source_paths
    assert "/etc/crontab" not in result2.clean_source_paths
    assert result2.partial is True

    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host=result2.host,
        clean_paths=result2.clean_source_paths,
        found_by_path=result2.found_by_source_path,
        now=utc_now_iso(),
    )
    assert soft_deleted == 0
    assert restored == 0

    row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row.soft_deleted_at is None


@pytest.mark.asyncio
async def test_emptied_readable_file_still_soft_deletes(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a source file that is READABLE but has had all its cron lines
    removed MUST still soft-delete its registered crons. Proves the
    STAGE-002-007A bugfix did not disable legitimate soft-delete."""
    cron_d_dir = tmp_path / "etc" / "cron.d"
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": "10 4 * * * root /storage/scripts/backup.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    result1 = await discoverer.scan(cron_repo, log=_NullLog())
    assert result1.inserted_count == 1
    (fp,) = tuple(result1.found_fingerprints)

    # Empty the file (still readable, just no cron lines — a comment remains).
    (cron_d_dir / "backup").write_text("# all jobs removed\n")

    result2 = await discoverer.scan(cron_repo, log=_NullLog())
    # Emptied-but-readable file has NO errors -> it IS clean, NOT unreadable.
    assert "/etc/cron.d/backup" not in result2.unreadable_source_paths
    assert "/etc/cron.d/backup" in result2.clean_source_paths

    soft_deleted, restored = await cron_repo.reconcile_soft_deletes(
        host=result2.host,
        clean_paths=result2.clean_source_paths,
        found_by_path=result2.found_by_source_path,
        now=utc_now_iso(),
    )
    # The cron is genuinely gone -> it MUST be soft-deleted.
    assert soft_deleted == 1
    assert restored == 0

    row = await repo.fetch_one(
        text("SELECT soft_deleted_at FROM crons WHERE fingerprint = :fp"),
        {"fp": fp},
    )
    assert row is not None
    assert row.soft_deleted_at is not None


@pytest.mark.asyncio
async def test_scan_result_unreadable_source_paths_field(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STAGE-002-007A bugfix: unreadable_source_paths is populated for each
    per-file read failure and is fully disjoint from clean_source_paths."""
    _make_host_tree(
        tmp_path,
        crontab="0 3 * * * root /storage/scripts/nightly.sh\n",
        cron_d_files={
            "readable": "10 4 * * * root /storage/scripts/ok.sh\n",
            "denied": "20 5 * * * root /storage/scripts/blocked.sh\n",
        },
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    discoverer = CronDiscoverer()

    # First scan readable so all three paths land in the DB.
    await discoverer.scan(cron_repo, log=_NullLog())

    # Now deny the cron.d/denied file only.
    _patch_read_text_permission_error(monkeypatch, deny_suffix="/cron.d/denied")
    result = await discoverer.scan(cron_repo, log=_NullLog())

    # Exactly the denied file is unreadable.
    assert result.unreadable_source_paths == frozenset({"/etc/cron.d/denied"})
    # The readable paths are still clean.
    assert "/etc/cron.d/readable" in result.clean_source_paths
    assert "/etc/crontab" in result.clean_source_paths
    # Invariant: clean and unreadable are disjoint.
    assert result.clean_source_paths.isdisjoint(result.unreadable_source_paths)
    assert result.partial is True


@pytest.mark.asyncio
async def test_discovered_cron_has_log_match_key(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovered cron row has a non-NULL log_match_key equal to canonical_log_key(command)."""
    command = "/storage/scripts/cron/backup.sh"
    _make_host_tree(
        tmp_path,
        cron_d_files={"backup": f"10 4 * * * root {command}\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")
    cron_repo = CronRepo(repo)
    result = await CronDiscoverer().scan(cron_repo, log=_NullLog())
    assert result.inserted_count == 1
    row = await repo.fetch_one(text("SELECT log_match_key, command FROM crons LIMIT 1"))
    assert row is not None
    assert row.log_match_key is not None
    assert row.log_match_key == canonical_log_key(row.command)


# ---------------------------------------------------------------------------
# _resolve_hostname: log object without warning method (line 73->79 branch)
# ---------------------------------------------------------------------------


def test_resolve_hostname_log_without_warning_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_hostname: when log has no 'warning' attr, branch at line 73 is False → skipped.

    This covers the 73->79 branch: the inner `if hasattr(log, "warning")` is False,
    so no warning is logged, but _hostname_fallback_warned is still set.
    """
    import homelab_monitor.plugins.discoverers.cron_discoverer as disc  # noqa: PLC0415

    # Reset global so the branch is entered
    monkeypatch.setattr(disc, "_hostname_fallback_warned", False)
    monkeypatch.delenv("HM_HOST_HOSTNAME", raising=False)

    # Pass a log object with NO warning method
    class _NoWarnLog:
        pass

    result = _resolve_hostname(_NoWarnLog())
    # Should return socket.gethostname() (no crash)
    import socket  # noqa: PLC0415

    assert result == socket.gethostname()
    # Flag should now be set
    assert disc._hostname_fallback_warned is True  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Option B fingerprint-stability regression (STAGE-002-009)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_user_cron_fingerprint_matches_crontab_source_path(
    repo: SqliteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user cron discovered from the snapshot dir has the SAME fingerprint
    that compute_fingerprint produces for source_path='crontab:<user>'.

    Locks the Option B invariant: swapping the read source (spool -> snapshot)
    must NOT change the identity tuple. source_path stays 'crontab:<user>'.
    """
    from homelab_monitor.kernel.cron.fingerprint import compute_fingerprint  # noqa: PLC0415

    _make_host_tree(
        tmp_path,
        user_crontabs={"jakekausler": "17 * * * * /storage/scripts/rtlamr.sh\n"},
    )
    monkeypatch.setenv("HM_CRON_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HM_CRON_SNAPSHOT_DIR", str(tmp_path / "crontab-snapshot"))
    monkeypatch.setenv("HM_HOST_HOSTNAME", "test-host")

    result = await CronDiscoverer().scan(CronRepo(repo), log=_NullLog())
    assert result.inserted_count == 1
    (fp,) = tuple(result.found_fingerprints)
    expected = compute_fingerprint(
        host="test-host",
        source_path="crontab:jakekausler",
        schedule="17 * * * *",
        command="/storage/scripts/rtlamr.sh",
    )
    assert fp == expected
