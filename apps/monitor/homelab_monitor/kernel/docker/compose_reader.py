"""Read-only docker-compose.yml reader (STAGE-003-009).

D-COMPOSE-READER-READ-ONLY: NEVER writes. Used by LocalBuildUpdateCollector
to find services that declare `build:` and resolve their build-context paths.
Reused later by STAGE-003-010 (Pull & Restart) to map container names to
compose service names.

Failures (file missing, malformed YAML, non-dict root) raise ComposeReadError
with a reason enum the collector emits as `check_error_reason`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import yaml

if TYPE_CHECKING:
    from structlog import BoundLogger

    from homelab_monitor.kernel.docker.path_resolver import PathResolver

ComposeReadFailureReason = Literal[
    "file_not_found",
    "malformed_yaml",
    "non_dict_root",
    "permission_denied",
    "unknown",
]


class ComposeReadError(ValueError):
    """Raised when a compose file cannot be parsed.

    `reason` is one of ComposeReadFailureReason; it is the value the
    collector persists as `check_error_reason` on per-container rows when
    this failure cascades down.
    """

    reason: ComposeReadFailureReason

    def __init__(self, message: str, *, reason: ComposeReadFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ComposeService:
    """One service entry parsed from a compose file."""

    name: str
    image: str | None
    build_context: Path | None  # absolute path; resolved relative to compose dir
    build_dockerfile: str | None
    profiles: tuple[str, ...]
    labels: dict[str, str] = field(default_factory=lambda: {})
    source_compose_path: Path | None = None  # which compose file this came from (for traceability)


@dataclass(frozen=True, slots=True)
class ComposeFile:
    """Top-level parsed compose file."""

    compose_path: Path  # absolute path to the compose file (for caller logging)
    services: dict[str, ComposeService]  # keyed by service name


_DEFAULT_DOCKERFILE: Final[str] = "Dockerfile"


def read_compose(path: Path) -> ComposeFile:
    """Parse a docker-compose.yml file.

    Raises:
        ComposeReadError: with `reason` set to one of ComposeReadFailureReason.
    """
    if not path.exists():
        msg = f"compose file not found: {path}"
        raise ComposeReadError(msg, reason="file_not_found")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        msg = f"permission denied reading {path}: {exc}"
        raise ComposeReadError(msg, reason="permission_denied") from exc
    except OSError as exc:  # pragma: no cover -- hardware/OS fault TOCTOU
        msg = f"failed to read {path}: {exc}"
        raise ComposeReadError(msg, reason="unknown") from exc

    try:
        raw_data: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        msg = f"malformed YAML in {path}: {exc}"
        raise ComposeReadError(msg, reason="malformed_yaml") from exc

    if not isinstance(raw_data, dict):
        msg = f"compose root is not a mapping in {path}"
        raise ComposeReadError(msg, reason="non_dict_root")

    data: dict[str, object] = cast("dict[str, object]", raw_data)
    services_raw: object = data.get("services") or {}
    if not isinstance(services_raw, dict):
        # An empty `services:` key, or `services: null`, is acceptable —
        # return a ComposeFile with no services.
        services_dict: dict[str, object] = {}
    else:
        services_dict = cast("dict[str, object]", services_raw)

    compose_dir = path.parent.resolve()
    services: dict[str, ComposeService] = {}
    for service_name, entry in services_dict.items():
        if not isinstance(entry, dict):
            continue
        name = str(service_name)
        entry_dict: dict[str, object] = cast("dict[str, object]", entry)
        services[name] = _parse_service(name, entry_dict, compose_dir=compose_dir)
    return ComposeFile(compose_path=path.resolve(), services=services)


def _parse_service(
    name: str,
    entry: dict[str, object],
    *,
    compose_dir: Path,
) -> ComposeService:
    image_raw = entry.get("image")
    image = str(image_raw) if isinstance(image_raw, str) else None

    build_context: Path | None = None
    build_dockerfile: str | None = None
    build_raw = entry.get("build")
    if isinstance(build_raw, str):
        # Shorthand: `build: ./path` — context = path, dockerfile = default.
        build_context = (compose_dir / build_raw).resolve()
        build_dockerfile = _DEFAULT_DOCKERFILE
    elif isinstance(build_raw, dict):
        build_dict: dict[str, object] = cast("dict[str, object]", build_raw)
        ctx_raw = build_dict.get("context")
        if isinstance(ctx_raw, str):
            build_context = (compose_dir / ctx_raw).resolve()
        df_raw = build_dict.get("dockerfile")
        if isinstance(df_raw, str):
            build_dockerfile = df_raw
        else:
            build_dockerfile = _DEFAULT_DOCKERFILE if build_context is not None else None

    profiles_raw = entry.get("profiles") or ()
    if isinstance(profiles_raw, list):
        profiles_list: list[object] = cast("list[object]", profiles_raw)
        profiles = tuple(str(p) for p in profiles_list)
    else:
        profiles = ()

    labels: dict[str, str] = {}
    labels_raw = entry.get("labels")
    if isinstance(labels_raw, dict):
        labels_dict: dict[str, object] = cast("dict[str, object]", labels_raw)
        for k, v in labels_dict.items():
            labels[str(k)] = str(v)
    elif isinstance(labels_raw, list):
        # docker-compose accepts `labels: ["k=v"]` form too.
        labels_list: list[object] = cast("list[object]", labels_raw)
        for item in labels_list:
            s = str(item)
            if "=" in s:
                k, _, v = s.partition("=")
                labels[k] = v

    return ComposeService(
        name=name,
        image=image,
        build_context=build_context,
        build_dockerfile=build_dockerfile,
        profiles=profiles,
        labels=labels,
    )


def read_compose_set(
    paths: Sequence[Path],
    *,
    path_resolver: PathResolver | None = None,
    log: BoundLogger | None = None,
) -> ComposeFile:
    """Read+merge N compose files. Later files override earlier ones.

    Per-file ComposeReadError is logged+skipped (partial load still valid).
    If EVERY file fails, raises the last error. If `path_resolver` is set,
    each service's build_context is remapped after parsing. Each merged
    service carries its `source_compose_path` for traceability.
    """
    if not paths:
        raise ComposeReadError(
            "read_compose_set requires at least one compose path",
            reason="file_not_found",
        )
    merged: dict[str, ComposeService] = {}
    last_error: ComposeReadError | None = None
    loaded_paths: list[Path] = []
    for p in paths:
        try:
            single = read_compose(p)
        except ComposeReadError as exc:
            last_error = exc
            if log is not None:
                log.warning(
                    "compose_reader.skip_file", path=str(p), reason=exc.reason, error=str(exc)
                )
            continue
        loaded_paths.append(p.resolve())
        for svc_name, svc in single.services.items():
            new_ctx = svc.build_context
            if new_ctx is not None and path_resolver is not None:
                new_ctx = path_resolver.resolve(new_ctx)
            merged[svc_name] = ComposeService(
                name=svc.name,
                image=svc.image,
                build_context=new_ctx,
                build_dockerfile=svc.build_dockerfile,
                profiles=svc.profiles,
                labels=dict(svc.labels),
                source_compose_path=p.resolve(),
            )
    if not loaded_paths and last_error is not None:
        raise last_error
    # impossible: if not loaded_paths and last_error is None, would have raised earlier
    return ComposeFile(compose_path=loaded_paths[-1], services=merged)


__all__ = [
    "ComposeFile",
    "ComposeReadError",
    "ComposeReadFailureReason",
    "ComposeService",
    "read_compose",
    "read_compose_set",
]
