"""Alertmanager configuration render-on-boot + reload."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final

import httpx
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.auth.api_tokens import make_api_token
from homelab_monitor.kernel.auth.repository import AuthRepository
from homelab_monitor.kernel.auth.scopes import Scope
from homelab_monitor.kernel.secrets.repository import AsyncSecretsRepository

#: Secret name for the rendered AM ingest token plaintext.
SECRET_NAME: Final[str] = "alertmanager-ingest-token"

#: API-token name (visible via ``hm api-token list``).
TOKEN_NAME: Final[str] = "alertmanager-ingest"

#: ``who`` field for audit rows on bootstrap mint.
BOOTSTRAP_WHO: Final[str] = "system:alertmanager-bootstrap"

#: Placeholder string in the template that gets substituted with the token.
TEMPLATE_PLACEHOLDER: Final[str] = "${ALERTMANAGER_INGEST_TOKEN}"

_HTTP_OK = 200


async def ensure_ingest_token(
    auth_repo: AuthRepository,
    secrets_repo: AsyncSecretsRepository,
    *,
    log: BoundLogger,
) -> str:
    """Return the AM ingest plaintext token; mint if absent.

    Looks up an existing token row named ``alertmanager-ingest`` AND its plaintext
    in the secrets store under ``alertmanager-ingest-token``. If BOTH are present,
    returns the plaintext. Otherwise, mints a fresh token + writes both rows.

    Side effects:
        - Writes one ``api_tokens`` row + one ``audit_log`` row (atomic, via
          ``AuthRepository.create_api_token``) on mint.
        - Writes one ``secrets`` row + one ``audit_log`` row (atomic, via
          ``AsyncSecretsRepository.set``) on mint.
        - On the existing-row path: zero side effects.

    Logging:
        - Emits ``alertmanager.bootstrap.token_reused`` (INFO) when an existing
          token is found.
        - Emits ``alertmanager.bootstrap.token_minted`` (INFO) when a new token
          is created. NEVER logs the plaintext.
    """
    existing_token = await auth_repo.get_api_token_by_name(TOKEN_NAME)
    existing_secret = await secrets_repo.get(SECRET_NAME)
    if existing_token is not None and existing_secret is not None:
        log.info("alertmanager.bootstrap.token_reused", token_name=TOKEN_NAME)
        return existing_secret

    # Pair is inconsistent (one present, one absent). Delete whichever exists
    # so the re-mint below can insert fresh rows without a UNIQUE collision.
    if existing_token is not None:
        await auth_repo.delete_api_token_by_name(TOKEN_NAME)
    if existing_secret is not None:
        await secrets_repo.delete(SECRET_NAME)

    plaintext, _sha = make_api_token()
    await auth_repo.create_api_token(
        name=TOKEN_NAME,
        scopes={Scope.ALERTS_INGEST_WRITE},
        plaintext_token=plaintext,
        who=BOOTSTRAP_WHO,
    )
    await secrets_repo.set(SECRET_NAME, plaintext, who=BOOTSTRAP_WHO)
    log.info("alertmanager.bootstrap.token_minted", token_name=TOKEN_NAME)
    return plaintext


def render_config(
    *,
    template_path: Path,
    output_path: Path,
    token: str,
    log: BoundLogger,
) -> None:
    """Render the AM template by substituting ``${ALERTMANAGER_INGEST_TOKEN}``.

    Atomic write: writes to a sibling ``.tmp`` file and ``os.replace``s the result
    so concurrent readers (i.e., AM picking up a SIGHUP) never see a partial file.

    Raises:
        FileNotFoundError: template_path does not exist.
        OSError: filesystem error on write/replace (caller logs warning).

    Logging:
        - ``alertmanager.render.success`` (INFO) on success with output_path.
        - ``alertmanager.render.failed`` (WARNING) on FileNotFoundError or OSError;
          re-raises so caller decides degrade vs abort.
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning(
            "alertmanager.render.failed",
            reason="template_missing",
            template_path=str(template_path),
        )
        raise
    rendered = template.replace(TEMPLATE_PLACEHOLDER, token)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: write to .tmp in same dir, then os.replace.
    fd, tmp_name = tempfile.mkstemp(
        prefix=output_path.name + ".",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp_name, output_path)
    except OSError:
        log.warning(
            "alertmanager.render.failed",
            reason="write_failed",
            output_path=str(output_path),
        )
        # Best-effort cleanup of the temp file; ignore secondary errors.
        with suppress(OSError):  # pragma: no cover -- defensive
            os.unlink(tmp_name)
        raise
    log.info(
        "alertmanager.render.success",
        output_path=str(output_path),
        bytes=len(rendered),
    )


class AlertmanagerReloader:
    """POST ``/-/reload`` to Alertmanager; warn-on-failure (file is source of truth)."""

    def __init__(
        self,
        *,
        am_url: str,
        http_client: httpx.AsyncClient,
        log: BoundLogger,
    ) -> None:
        self._am_url = am_url.rstrip("/")
        self._http = http_client
        self._log = log

    async def reload(self) -> bool:
        """POST ``/-/reload``; return True on 200, False on any failure (logged)."""
        url = f"{self._am_url}/-/reload"
        try:
            resp = await self._http.post(url, timeout=httpx.Timeout(5.0, connect=2.0))
        except httpx.HTTPError as exc:
            self._log.warning(
                "alertmanager.reload.unreachable",
                am_url=self._am_url,
                error=str(exc),
            )
            return False
        if resp.status_code != _HTTP_OK:
            self._log.warning(
                "alertmanager.reload.non_200",
                am_url=self._am_url,
                status_code=resp.status_code,
            )
            return False
        self._log.info("alertmanager.reload.ok", am_url=self._am_url)
        return True


async def render_on_boot(  # noqa: PLR0913 -- explicit DI for testability
    *,
    auth_repo: AuthRepository,
    secrets_repo: AsyncSecretsRepository,
    template_path: Path,
    output_path: Path,
    am_url: str | None,
    http_client: httpx.AsyncClient,
    log: BoundLogger,
) -> None:
    """Top-level boot orchestration: ensure token → render → (optional) reload.

    NEVER raises on any failure path. Failures are logged and swallowed so
    lifespan continues:
      - ensure_ingest_token failures: logged at ERROR with traceback (rare;
        indicates DB or secrets store breakage).
      - render_config failures: logged at WARNING (template missing or disk
        full).
      - reload failures: logged at WARNING (file is source of truth; AM picks
        it up at next start).

    This is the function called from lifespan. ``am_url`` may be ``None`` to
    skip the reload step entirely (used in tests + first-boot when AM isn't
    yet up).
    """
    try:
        token = await ensure_ingest_token(auth_repo, secrets_repo, log=log)
    except Exception as exc:
        log.error("alertmanager.bootstrap.failed", error=str(exc), exc_info=True)
        return
    try:
        render_config(
            template_path=template_path,
            output_path=output_path,
            token=token,
            log=log,
        )
    except (FileNotFoundError, OSError):
        return  # already logged inside render_config
    if am_url is not None:
        reloader = AlertmanagerReloader(am_url=am_url, http_client=http_client, log=log)
        await reloader.reload()
