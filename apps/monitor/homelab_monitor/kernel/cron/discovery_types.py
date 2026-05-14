"""Shared types for cron discovery (STAGE-002-007)."""

from __future__ import annotations

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
    INSERTed. `last_discovered_at` bumps still apply. 007A's soft-delete
    reconciliation gates on `partial=False`.
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


__all__ = [
    "CronScanError",
    "CronScanResult",
    "CronSourceKind",
    "ParsedCronEntry",
]
