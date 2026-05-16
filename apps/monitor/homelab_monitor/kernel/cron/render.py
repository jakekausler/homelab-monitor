"""Cron-events Vector configuration render-on-boot (STAGE-002-008 fix).

Mirrors kernel/alertmanager/render.py::render_config / render_on_boot, MINUS
the Alertmanager /-/reload step: Vector picks up the rendered config at
container start (depends_on: monitor: service_healthy), so no reload is needed.

The monitor renders deploy/vector/vector.toml.template to a shared named
volume with the cron-events ingest token substituted in, so a first-ever
`docker compose up -d` needs zero manual token-paste steps.
"""

from __future__ import annotations

import grp
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.cron.log_ingest_token import ensure_cron_events_token
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

#: Placeholder string in vector.toml.template substituted with the token.
TEMPLATE_PLACEHOLDER: Final[str] = "${CRON_EVENTS_INGEST_TOKEN}"

#: GID-shared group name used to chown the rendered config so Vector (joining
#: the group via compose ``group_add``) can read the 0640 file. Reused from
#: the Alertmanager render mechanism — the Dockerfile already creates it.
CONFIG_GROUP_NAME: Final[str] = "amconfig"


def render_config(
    *,
    template_path: Path,
    output_path: Path,
    token: str,
    log: BoundLogger,
) -> None:
    """Render vector.toml by substituting ``${CRON_EVENTS_INGEST_TOKEN}``.

    Atomic write: writes to a sibling ``.tmp`` file and ``os.replace``s the
    result so a concurrently-starting Vector never reads a partial file.

    Raises:
        FileNotFoundError: template_path does not exist.
        OSError: filesystem error on write/replace (caller logs warning).

    Logging:
        - ``cron_events.render.success`` (INFO) on success with output_path.
        - ``cron_events.render.failed`` (WARNING) on FileNotFoundError or
          OSError; re-raises so caller decides degrade vs abort.
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error(
            "cron_events.render.failed",
            reason="template_missing",
            template_path=str(template_path),
            consequence="vector.toml not rendered — Vector will fail to start",
        )
        raise
    rendered = template.replace(TEMPLATE_PLACEHOLDER, token)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: write to .tmp in same dir, then os.replace.
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=output_path.name + ".",
            suffix=".tmp",
            dir=str(output_path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp_name, output_path)
        # Group-own the file by the amconfig GID so Vector (running with a
        # supplementary GID via compose group_add) can read it. Mode 0o640
        # keeps the bearer token unreadable by other users. If the group
        # doesn't exist (unit-test environment without the docker-built
        # image), keep the process default GID and 0o640 — the file is only
        # readable by uid + primary gid, which is fine for tests since they
        # don't bind-mount the file to Vector.
        try:
            gid = grp.getgrnam(CONFIG_GROUP_NAME).gr_gid
        except KeyError:
            pass
        else:
            try:
                os.chown(output_path, -1, gid)
            except OSError as exc:
                log.warning(
                    "cron_events.render.chown_failed",
                    output_path=str(output_path),
                    target_gid=gid,
                    reason=str(exc),
                )
        os.chmod(output_path, 0o640)
    except OSError as exc:
        log.error(
            "cron_events.render.failed",
            reason="write_failed",
            output_path=str(output_path),
            output_dir=str(output_path.parent),
            error=str(exc),
            consequence="vector.toml not rendered — Vector will fail to start",
        )
        # Best-effort cleanup of the temp file; ignore secondary errors.
        if tmp_name is not None:
            with suppress(OSError):  # pragma: no cover -- defensive
                os.unlink(tmp_name)
        raise
    log.info(
        "cron_events.render.success",
        output_path=str(output_path),
        bytes=len(rendered),
    )


async def render_on_boot(
    *,
    auth_repo: AuthRepository,
    secrets_repo: AsyncSecretsRepository,
    template_path: Path,
    output_path: Path,
    log: BoundLogger,
) -> str | None:
    """Top-level boot orchestration: ensure token -> render.

    NEVER raises on any failure path. Failures are logged and swallowed so
    lifespan continues:
      - ensure_cron_events_token failures: logged at ERROR with traceback
        (rare; indicates DB or secrets store breakage).
      - render_config failures: logged at WARNING (template missing or disk
        full).

    Unlike the Alertmanager renderer there is NO reload step: Vector reads the
    rendered config at container start (depends_on: monitor: service_healthy).

    Returns the minted/reused plaintext token on success so the caller can
    stash it on ``app.state`` (parity with the prior ``# 8c`` block), or
    ``None`` if token bootstrap failed.
    """
    try:
        token = await ensure_cron_events_token(auth_repo, secrets_repo, log=log)
    except Exception as exc:
        log.error("cron_events.bootstrap.failed", error=str(exc), exc_info=True)
        return None
    try:
        render_config(
            template_path=template_path,
            output_path=output_path,
            token=token,
            log=log,
        )
    except (FileNotFoundError, OSError):
        # already logged inside render_config; still return the token so the
        # caller can stash it (the render failure is degrade-not-abort).
        return token
    return token


__all__ = [
    "CONFIG_GROUP_NAME",
    "TEMPLATE_PLACEHOLDER",
    "render_config",
    "render_on_boot",
]
