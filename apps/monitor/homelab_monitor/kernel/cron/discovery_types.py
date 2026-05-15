"""Shared types for cron discovery (STAGE-002-007)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class CronSourceKind(StrEnum):
    """How to parse a given crontab-format file.

    SYSTEM_WITH_USER_FIELD: ``/etc/crontab`` and ``/etc/cron.d/*`` — each line
        has a USER field between schedule and command.
    USER_CRONTAB: ``/var/spool/cron/crontabs/<user>`` — no USER field; the
        username is the filename.
    """

    SYSTEM_WITH_USER_FIELD = "system_with_user_field"
    USER_CRONTAB = "user_crontab"


@dataclass(slots=True, frozen=True)
class ParsedCronEntry:
    """One parsed cron line.

    `host_source_path` is the HOST path (e.g., ``/etc/cron.d/certbot``,
    ``crontab:jakekausler``) — NOT the container path ``/host/etc/...``.
    The decoupling is critical for wrapper-installer convergence: the wrapper
    commits to host paths, and the fingerprint must match.
    """

    host: str
    host_source_path: str  # e.g. "/etc/crontab", "/etc/cron.d/certbot", "crontab:alice"
    schedule: str  # raw expression as it appears on disk (per D7)
    command: str


@dataclass(slots=True, frozen=True)
class CronScanError:
    """A non-fatal error encountered while scanning."""

    host_source_path: str  # the path that errored
    error: str  # short human description (e.g., "permission denied", "invalid schedule: '*/x'")


@dataclass(slots=True, frozen=True)
class CronScanResult:
    """Result of one discovery scan (consumed by STAGE-002-007A reconciliation).

    `partial=True` means at least one source file errored (per-file or
    per-line). New fingerprints from successfully-read files are still
    INSERTed. `last_discovered_at` bumps still apply.

    STAGE-002-007A per-source-file reconciliation fields:
    - `host`: the local hostname this scan ran for (from _resolve_hostname).
      Reconciliation needs it for the per-host WHERE filter.
    - `clean_source_paths`: host source paths (e.g. "/etc/crontab",
      "/etc/cron.d/foo", "crontab:alice") that were cleanly inspected this
      scan, INCLUDING absent-but-known paths whose parent directory iterated
      cleanly. Reconciliation runs only for paths in this set.
    - `unreachable_source_path_prefixes`: parent-dir prefixes whose iterdir
      itself failed (e.g. "/etc/cron.d"). Informational; paths under these
      prefixes are kept out of clean_source_paths.
    - `unreadable_source_paths`: any host source path that did NOT cleanly
      inspect this scan — whether the file-level read failed (PermissionError /
      OSError) OR the file had a per-line parse error. DISTINCT from
      unreachable_source_path_prefixes (which is whole-directory iterdir
      failures). Such a path must NEVER be in clean_source_paths; if it were,
      reconciliation would soft-delete its DB rows on an incomplete observation
      (STAGE-002-007A data-corruption bugfix).
    - `found_by_source_path`: fingerprints grouped by host source path. A path
      present in clean_source_paths but absent as a key here means the file
      was cleanly inspected and contained zero crons (all its DB rows are
      soft-delete candidates).
    """

    found_fingerprints: frozenset[str] = field(
        default_factory=lambda: frozenset[str](),  # noqa: PLW0108
    )
    partial: bool = False
    errors: list[CronScanError] = field(
        default_factory=lambda: list[CronScanError](),  # noqa: PLW0108
    )
    inserted_count: int = 0
    updated_count: int = 0  # non-bump field updates only (audit-row-emitting)
    bump_only_count: int = 0  # existing rows whose only change was last_discovered_at
    host: str = ""
    clean_source_paths: frozenset[str] = field(
        default_factory=lambda: frozenset[str](),  # noqa: PLW0108
    )
    unreachable_source_path_prefixes: frozenset[str] = field(
        default_factory=lambda: frozenset[str](),  # noqa: PLW0108
    )
    unreadable_source_paths: frozenset[str] = field(
        default_factory=lambda: frozenset[str](),  # noqa: PLW0108
    )
    found_by_source_path: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: dict[str, frozenset[str]](),  # noqa: PLW0108
    )


__all__ = [
    "CronScanError",
    "CronScanResult",
    "CronSourceKind",
    "ParsedCronEntry",
]
