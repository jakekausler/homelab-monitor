"""Tests for kernel/cron/render.py — Vector config render-on-boot.

Mirrors test_alertmanager_render.py. The cron renderer has NO reload step.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest
import structlog

from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.cron.log_ingest_token import (
    SECRET_NAME,
    TOKEN_NAME,
)
from homelab_monitor.kernel.cron.render import (
    TEMPLATE_PLACEHOLDER,
    render_config,
    render_on_boot,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

# --- render_config -------------------------------------------------------


def test_render_config_substitutes_placeholder(tmp_path: Path) -> None:
    """Template has placeholder, rendered output contains the actual token."""
    template_file = tmp_path / "vector.toml.template"
    output_file = tmp_path / "vector.toml"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')

    log = structlog.get_logger()
    token = "test_token_12345"

    render_config(
        template_path=template_file,
        output_path=output_file,
        token=token,
        log=log,  # type: ignore[arg-type]
    )

    assert output_file.exists()
    content = output_file.read_text()
    assert token in content
    assert TEMPLATE_PLACEHOLDER not in content


def test_render_config_template_missing_raises(tmp_path: Path) -> None:
    """Template path absent -> FileNotFoundError raised + warning log."""
    template_file = tmp_path / "nonexistent.toml.template"
    output_file = tmp_path / "vector.toml"
    log = structlog.get_logger()

    with pytest.raises(FileNotFoundError):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="token",
            log=log,  # type: ignore[arg-type]
        )


def test_render_config_creates_parent_dirs(tmp_path: Path) -> None:
    """Output parent doesn't exist -> directory created and file written."""
    template_file = tmp_path / "vector.toml.template"
    output_file = tmp_path / "nested" / "dir" / "vector.toml"
    template_file.write_text(f'key = "{TEMPLATE_PLACEHOLDER}"\n')
    log = structlog.get_logger()

    assert not output_file.parent.exists()

    render_config(
        template_path=template_file,
        output_path=output_file,
        token="test_token",
        log=log,  # type: ignore[arg-type]
    )

    assert output_file.parent.exists()
    assert output_file.exists()


def test_render_config_group_missing(tmp_path: Path) -> None:
    """grp.getgrnam raises KeyError (amconfig group absent) -> file still written."""
    template_file = tmp_path / "vector.toml.template"
    output_file = tmp_path / "vector.toml"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    log = structlog.get_logger()

    with (
        mock.patch(
            "homelab_monitor.kernel.cron.render.grp.getgrnam",
            side_effect=KeyError("amconfig"),
        ),
        mock.patch("homelab_monitor.kernel.cron.render.os.chown"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore[arg-type]
        )

    assert output_file.exists()
    assert "test-token" in output_file.read_text()


def test_render_config_group_present_chowns_file(tmp_path: Path) -> None:
    """grp.getgrnam returns a group -> file is chowned to that group's gid."""
    template_file = tmp_path / "vector.toml.template"
    output_file = tmp_path / "vector.toml"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    log = structlog.get_logger()

    fake_group = mock.MagicMock()
    fake_group.gr_gid = 2000

    with (
        mock.patch(
            "homelab_monitor.kernel.cron.render.grp.getgrnam",
            return_value=fake_group,
        ),
        mock.patch("homelab_monitor.kernel.cron.render.os.chown") as mock_chown,
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore[arg-type]
        )

    mock_chown.assert_called_once_with(output_file, -1, 2000)
    assert output_file.exists()
    assert "test-token" in output_file.read_text()


def test_render_config_chown_oserror_logged(tmp_path: Path) -> None:
    """os.chown raises OSError -> warning logged, file still written, no raise."""
    template_file = tmp_path / "vector.toml.template"
    output_file = tmp_path / "vector.toml"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    log = structlog.get_logger()

    fake_group = mock.MagicMock()
    fake_group.gr_gid = 2000

    with (
        mock.patch(
            "homelab_monitor.kernel.cron.render.grp.getgrnam",
            return_value=fake_group,
        ),
        mock.patch(
            "homelab_monitor.kernel.cron.render.os.chown",
            side_effect=PermissionError("test no chown cap"),
        ),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token-chown-fail",
            log=log,  # type: ignore[arg-type]
        )

    assert output_file.exists()
    assert "test-token-chown-fail" in output_file.read_text()


def test_render_config_raises_on_write_failure(tmp_path: Path) -> None:
    """OSError during atomic replace -> log warning + re-raise."""
    template_file = tmp_path / "vector.toml.template"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    output_file = tmp_path / "vector.toml"
    log = structlog.get_logger()

    with (
        mock.patch("os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token-plaintext",
            log=log,  # type: ignore[arg-type]
        )


def test_render_config_cleans_up_tmp_file_on_replace_failure(tmp_path: Path) -> None:
    """OSError during os.replace -> temp file is unlinked, then re-raised."""
    template_file = tmp_path / "vector.toml.template"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    output_file = tmp_path / "vector.toml"
    log = structlog.get_logger()

    tmp_files_seen: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def _spy_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        fd, path = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        tmp_files_seen.append(path)
        return fd, path

    with (
        mock.patch("tempfile.mkstemp", side_effect=_spy_mkstemp),
        mock.patch("os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        render_config(
            template_path=template_file,
            output_path=output_file,
            token="test-token",
            log=log,  # type: ignore[arg-type]
        )

    assert len(tmp_files_seen) == 1
    assert not Path(tmp_files_seen[0]).exists(), "temp file should have been unlinked"


# --- render_on_boot ------------------------------------------------------


@pytest.mark.asyncio
async def test_render_on_boot_full_happy_path(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path
) -> None:
    """Fresh DB + valid template -> renders, returns minted token."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    template_path = tmp_path / "vector.toml.template"
    output_path = tmp_path / "vector.toml"
    template_path.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')

    token = await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        log=log,  # type: ignore[arg-type]
    )

    assert token is not None
    # Token is minted + persisted.
    token_row = await auth_repo.get_api_token_by_name(TOKEN_NAME)
    assert token_row is not None
    secret_value = await secrets_repo.get(SECRET_NAME)
    assert secret_value == token
    # Output rendered with placeholder substituted.
    assert output_path.exists()
    content = output_path.read_text()
    assert TEMPLATE_PLACEHOLDER not in content
    assert token in content


@pytest.mark.asyncio
async def test_render_on_boot_swallows_render_errors(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path
) -> None:
    """Missing template -> returns the token (no raise), logs warning."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    template_path = tmp_path / "nonexistent.toml.template"
    output_path = tmp_path / "vector.toml"

    token = await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        log=log,  # type: ignore[arg-type]
    )

    # Token still minted even though render failed (degrade-not-abort).
    assert token is not None
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_render_on_boot_swallows_ensure_token_failure(tmp_path: Path) -> None:
    """ensure_cron_events_token raising -> render_on_boot logs error, returns None."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    log = structlog.get_logger()
    template_file = tmp_path / "vector.toml.template"
    template_file.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')
    output_file = tmp_path / "vector.toml"

    with mock.patch(
        "homelab_monitor.kernel.cron.render.ensure_cron_events_token",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await render_on_boot(
            auth_repo=None,  # type: ignore[arg-type]
            secrets_repo=None,  # type: ignore[arg-type]
            template_path=template_file,
            output_path=output_file,
            log=log,  # type: ignore[arg-type]
        )

    assert result is None
    assert not output_file.exists()


@pytest.mark.asyncio
async def test_render_on_boot_idempotent_token_reuse(
    repo: SqliteRepository, master_key: bytes, tmp_path: Path
) -> None:
    """Second render_on_boot reuses the same token (idempotent)."""
    auth_repo = AuthRepository(repo)
    secrets_repo = AsyncSecretsRepository(repo, master_key)
    log = structlog.get_logger()

    template_path = tmp_path / "vector.toml.template"
    output_path = tmp_path / "vector.toml"
    template_path.write_text(f'token = "{TEMPLATE_PLACEHOLDER}"\n')

    token1 = await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        log=log,  # type: ignore[arg-type]
    )
    token2 = await render_on_boot(
        auth_repo=auth_repo,
        secrets_repo=secrets_repo,
        template_path=template_path,
        output_path=output_path,
        log=log,  # type: ignore[arg-type]
    )

    assert token1 is not None
    assert token1 == token2
