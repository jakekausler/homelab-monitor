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
from homelab_monitor.kernel.config import RedactPattern, load_redact_patterns
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
        redact_vrl: The generated redact remap VRL body (from
            ``build_redact_vrl``). Substituted VERBATIM into the
            ``${VECTOR_REDACT_TRANSFORMS}`` placeholder inside BOTH the
            ``redact_main`` and ``redact_hmrun`` transform source bodies in
            the template, so both paths run byte-identical VRL.
        redact_strip_markers: The generated del(.rdt_<name>) body (from
            ``build_redact_strip_markers``). Substituted into
            ``${VECTOR_REDACT_STRIP_MARKERS}``.
        redact_metrics: The generated [[transforms.redaction_metric.metrics]]
            TOML entries (from ``build_redact_metric_entries``). Substituted
            into ``${VECTOR_REDACT_METRICS}``.
        json_max_depth: Approximate dotted-segment depth cap for json_flatten.
            Substituted as a bare integer into
            ``${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}``.
        json_max_fields: Hard field cap for json_flatten (sets ``.json._truncated``
            when exceeded). Substituted as a bare integer into
            ``${HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS}``.
    """

    cron_events_token: str
    docker_exclude_csv: str
    redact_vrl: str = ""
    redact_strip_markers: str = ""
    redact_metrics: str = ""
    json_max_depth: str = "8"
    json_max_fields: str = "100"


def _escape_regex_for_vrl_raw_string(pattern: str) -> str:
    """Escape a regex pattern for embedding in a VRL raw-string regex arg r'...'.

    VRL slash-delimited regex literals (/.../) are NOT usable in Vector 0.41.1 —
    the lexer rejects '\\s', '?', quotes, etc. (E203/E202). The working form, used
    by every other transform (docker_severity_extract, cron_parsed, hmrun_shaped),
    is the raw-string regex argument r'...'. r'...' is single-quote delimited and
    has NO escape char, so a literal "'" would terminate the string; Rust's regex
    crate accepts \\x27 (a single quote) which contains no literal quote. Slashes
    need NO escaping in raw-string regex args.
    """
    return pattern.replace("'", r"\x27")


def build_redact_vrl(patterns: list[RedactPattern]) -> str:
    """Generate the shared redact remap VRL body for the given patterns.

    Per pattern, the API-correct idiom (match() detect → replace() → marker):

        if match(to_string(.message) ?? "", r'<pattern with \\x27 for quotes>') {
          .message = replace(to_string(.message) ?? "", r'<pattern>', "<replacement>")
          .rdt_<name> = 1
        }

    ``match()`` is INFALLIBLE (no `!`). The .rdt_<name> marker is an integer (0
    default, 1 on match — always set so log_to_metric's field always exists) the
    downstream log_to_metric taps; strip_markers del()s it before VL. The body
    is identical for the main and hmrun paths (drift-guard enforced).

    Empty pattern list → a no-op body ("# no redaction patterns configured\\n")
    so the template still renders valid TOML.
    """
    if not patterns:
        return "# no redaction patterns configured\n"
    blocks: list[str] = []
    # Initialize every marker to 0 so log_to_metric's field=rdt_<name> always
    # exists (Vector 0.41 log_to_metric errors+drops the event when the field is
    # absent — it does NOT skip). Integer 0/1 (NOT boolean) because log_to_metric
    # parses the field as a float; booleans raise "Failed to parse field as float".
    blocks.extend(f".rdt_{p.name} = 0" for p in patterns)
    for p in patterns:
        lit = _escape_regex_for_vrl_raw_string(p.pattern)
        repl = p.replacement.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
        blocks.append(
            f"if match(to_string(.message) ?? \"\", r'{lit}') {{\n"
            f'  .message = replace(to_string(.message) ?? "", r\'{lit}\', "{repl}")\n'
            f"  .rdt_{p.name} = 1\n"
            f"}}"
        )
    return "\n".join(blocks) + "\n"


def build_redact_strip_markers(patterns: list[RedactPattern]) -> str:
    """Generate the del(.rdt_<name>) body that strips markers before VL.

    Identical for the main and hmrun strip transforms (drift-guarded). del()
    is a VRL no-op on absent fields, so unconditional del is safe.
    """
    if not patterns:
        return "# no redaction markers to strip\n"
    return "\n".join(f"del(.rdt_{p.name})" for p in patterns) + "\n"


def build_redact_metric_entries(patterns: list[RedactPattern]) -> str:
    """Generate the [[transforms.redaction_metric.metrics]] TOML entries.

    One counter entry per pattern: increments vector_redactions_total when
    .rdt_<name> is present; tags.pattern_type is a STATIC literal (the pattern
    name), never matched text.
    """
    if not patterns:
        return "# no redaction metrics configured"
    entries: list[str] = []
    for p in patterns:
        entries.append(
            "[[transforms.redaction_metric.metrics]]\n"
            'type = "counter"\n'
            f'field = "rdt_{p.name}"\n'
            'name = "vector_redactions_total"\n'
            "increment_by_value = true\n"
            f'tags.pattern_type = "{p.name}"'
        )
    return "\n\n".join(entries)


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
    - ``${VECTOR_REDACT_TRANSFORMS}`` → context.redact_vrl (generated redact body, used in
      BOTH redact_main and redact_hmrun)
    - ``${VECTOR_REDACT_STRIP_MARKERS}`` → context.redact_strip_markers (generated del body)
    - ``${VECTOR_REDACT_METRICS}`` → context.redact_metrics (generated metric entries)
    - ``${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}`` → context.json_max_depth (bare integer)
    - ``${HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS}`` → context.json_max_fields (bare integer)

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
        "${VECTOR_REDACT_TRANSFORMS}": context.redact_vrl,
        "${VECTOR_REDACT_STRIP_MARKERS}": context.redact_strip_markers,
        "${VECTOR_REDACT_METRICS}": context.redact_metrics,
        "${HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH}": context.json_max_depth,
        "${HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS}": context.json_max_fields,
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
    try:
        redact_patterns = load_redact_patterns()
    except Exception as exc:  # config error → render empty (no redaction) but log loudly
        log.error(
            "vector.redact.config_invalid",
            error=str(exc),
            consequence="redaction patterns NOT applied this boot (config rejected)",
        )
        redact_patterns = []
    redact_vrl = build_redact_vrl(redact_patterns)
    redact_strip_markers = build_redact_strip_markers(redact_patterns)
    redact_metrics = build_redact_metric_entries(redact_patterns)
    # Coerce via int(...) so a non-integer env value fails fast at boot rather than
    # rendering a literal "${...}"-or-garbage into the VRL (which would crash-loop
    # Vector with an opaque parse error). The coerced int is re-stringified for
    # substitution (the placeholder renders as a bare integer literal in VRL).
    json_max_depth = str(int(os.environ.get("HOMELAB_MONITOR_LOG_JSON_MAX_DEPTH", "8")))
    json_max_fields = str(int(os.environ.get("HOMELAB_MONITOR_LOG_JSON_MAX_FIELDS", "100")))
    context = VectorRenderContext(
        cron_events_token=token,
        docker_exclude_csv=docker_exclude_csv,
        redact_vrl=redact_vrl,
        redact_strip_markers=redact_strip_markers,
        redact_metrics=redact_metrics,
        json_max_depth=json_max_depth,
        json_max_fields=json_max_fields,
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
    "build_redact_metric_entries",
    "build_redact_strip_markers",
    "build_redact_vrl",
    "render_config",
    "render_on_boot",
]
