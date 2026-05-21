"""Vector configuration render-on-boot.

Originally introduced in STAGE-002-008 (cron-events ingest token substitution).
Extended in STAGE-003-002 to also substitute the docker_logs ``exclude_containers``
list from the ``VECTOR_DOCKER_EXCLUDE`` env var.

Mirrors kernel/alertmanager/render.py::render_config / render_on_boot, MINUS
the Alertmanager /-/reload step: Vector picks up the rendered config at
container start (depends_on: monitor: service_healthy), so no reload is needed.

The monitor renders deploy/vector/vector.toml.template to a shared named
volume with all required placeholders substituted in, so a first-ever
``docker compose up -d`` needs zero manual token-paste steps.
"""

from __future__ import annotations

import grp
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.cron.log_ingest_token import ensure_cron_events_token
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

#: Placeholder string in vector.toml.template substituted with the token.
#: Kept for backward compatibility — callers importing this constant continue to work.
TEMPLATE_PLACEHOLDER: Final[str] = "${CRON_EVENTS_INGEST_TOKEN}"

#: GID-shared group name used to chown the rendered config so Vector (joining
#: the group via compose ``group_add``) can read the 0640 file.
CONFIG_GROUP_NAME: Final[str] = "amconfig"


@dataclass(frozen=True)
class VectorRenderContext:
    """All values needed to render vector.toml from the template.

    Attributes:
        cron_events_token: Plaintext API token for the cron-events ingest
            endpoint. Substituted into ``${CRON_EVENTS_INGEST_TOKEN}``.
        docker_exclude_csv: Raw ``VECTOR_DOCKER_EXCLUDE`` env-var value (may
            be empty). Passed through ``csv_to_toml_array`` to render the
            ``${VECTOR_DOCKER_EXCLUDE}`` TOML array literal.
    """

    cron_events_token: str
    docker_exclude_csv: str


def csv_to_toml_array(csv: str, log: BoundLogger) -> str:
    """Convert a CSV string of container names to a TOML array literal.

    Empty or whitespace-only CSV → ``"[]"``
    Trims whitespace per entry; drops empty entries (including those produced
    by trailing commas or doubled commas).

    Entries containing ``"`` or ``\\`` are skipped with a WARNING log naming
    the offending entry; Vector still starts with the remaining valid entries.

    Returns:
        ``"[]"`` when no valid entries remain, otherwise
        ``'["name1","name2"]'``.
    """
    if not csv.strip():
        return "[]"

    valid: list[str] = []
    for raw in csv.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if '"' in entry or "\\" in entry:
            log.warning(
                "vector_docker_exclude.invalid_entry",
                entry=entry,
                reason="entry contains quote or backslash; skipping",
            )
            continue
        valid.append(entry)

    if not valid:
        return "[]"
    inner = '", "'.join(valid)
    return f'["{inner}"]'


def render_config(
    *,
    template_path: Path,
    output_path: Path,
    context: VectorRenderContext,
    log: BoundLogger,
) -> None:
    """Render vector.toml by substituting all template placeholders.

    Placeholders substituted:
    - ``${CRON_EVENTS_INGEST_TOKEN}`` → context.cron_events_token
    - ``${VECTOR_DOCKER_EXCLUDE}`` → TOML array literal from context.docker_exclude_csv

    Atomic write: writes to a sibling ``.tmp`` file and ``os.replace``s the
    result so a concurrently-starting Vector never reads a partial file.

    Raises:
        FileNotFoundError: template_path does not exist.
        OSError: filesystem error on write/replace (caller logs warning).

    Logging:
        - ``vector.render.success`` (INFO) on success with output_path.
        - ``vector.render.failed`` (WARNING/ERROR) on failure; re-raises.
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error(
            "vector.render.failed",
            reason="template_missing",
            template_path=str(template_path),
            consequence="vector.toml not rendered — Vector will fail to start",
        )
        raise

    substitutions: dict[str, str] = {
        "${CRON_EVENTS_INGEST_TOKEN}": context.cron_events_token,
        "${VECTOR_DOCKER_EXCLUDE}": csv_to_toml_array(context.docker_exclude_csv, log),
    }
    rendered = template
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

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
        # image), keep the process default GID and 0o640.
        try:
            gid = grp.getgrnam(CONFIG_GROUP_NAME).gr_gid
        except KeyError:
            pass
        else:
            try:
                os.chown(output_path, -1, gid)
            except OSError as exc:
                log.warning(
                    "vector.render.chown_failed",
                    output_path=str(output_path),
                    target_gid=gid,
                    reason=str(exc),
                )
        os.chmod(output_path, 0o640)
    except OSError as exc:
        log.error(
            "vector.render.failed",
            reason="write_failed",
            output_path=str(output_path),
            output_dir=str(output_path.parent),
            error=str(exc),
            consequence="vector.toml not rendered — Vector will fail to start",
        )
        if tmp_name is not None:
            with suppress(OSError):  # pragma: no cover -- defensive
                os.unlink(tmp_name)
        raise
    log.info(
        "vector.render.success",
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

    Reads ``VECTOR_DOCKER_EXCLUDE`` from the process environment (default ``""``
    = tail ALL containers). Builds a VectorRenderContext and calls render_config.

    NEVER raises on any failure path. Failures are logged and swallowed so
    lifespan continues:
      - ensure_cron_events_token failures: logged at ERROR with traceback.
      - render_config failures: logged at WARNING (template missing or disk full).

    Returns the minted/reused plaintext token on success, or ``None`` if token
    bootstrap failed.
    """
    try:
        token = await ensure_cron_events_token(auth_repo, secrets_repo, log=log)
    except Exception as exc:
        log.error("cron_events.bootstrap.failed", error=str(exc), exc_info=True)
        return None

    docker_exclude_csv = os.environ.get("VECTOR_DOCKER_EXCLUDE", "")
    context = VectorRenderContext(
        cron_events_token=token,
        docker_exclude_csv=docker_exclude_csv,
    )
    try:
        render_config(
            template_path=template_path,
            output_path=output_path,
            context=context,
            log=log,
        )
    except (FileNotFoundError, OSError):
        # already logged inside render_config; still return the token
        return token
    return token


__all__ = [
    "CONFIG_GROUP_NAME",
    "TEMPLATE_PLACEHOLDER",
    "VectorRenderContext",
    "render_config",
    "render_on_boot",
]
