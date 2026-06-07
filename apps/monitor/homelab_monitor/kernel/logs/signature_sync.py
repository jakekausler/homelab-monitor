"""SignatureCatalogSync — drain cycle -> log_signatures upsert (STAGE-004-028, A1).

Touched-only: only the signatures observed in the just-completed drain cycle are
upserted (the consumer's per-cycle accumulators). `label` + `status` are NEVER
written (user-owned). `total_count` accumulates the per-cycle line delta. On INSERT,
first_seen_at is set; on UPDATE it is preserved. last_seen_at = the cycle's newest
line ts (max_ts_seen).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from homelab_monitor.kernel.db.repository import SqliteRepository

# Accumulator value shape threaded from the consumer:
#   sig_state[(model_key, template_hash)] = (cluster_size, first_seen_ts, template_str)
SigStateValue = tuple[int, int, str]

# Highest-severity-wins ordering for first_seen_severity (lower index = higher rank).
_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "error": 1,
    "warning": 2,
    "notice": 3,
    "info": 4,
    "debug": 5,
    "unknown": 6,
}


def _pick_first_seen_severity(severities: list[str]) -> str:
    """Pick the highest-ranked severity from a signature's first-cycle counts.

    Unknown severities sort after all ranked ones (then alphabetically) so the
    result is deterministic regardless of dict iteration order.
    """
    return min(severities, key=lambda s: (_SEVERITY_RANK.get(s, len(_SEVERITY_RANK)), s))


class SignatureCatalogSync:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def sync_cycle(
        self,
        *,
        sig_state: dict[tuple[str, str], SigStateValue],
        cycle_counts: dict[tuple[str, str, str], int],
        last_seen_at: int,
    ) -> None:
        """Upsert every signature touched this cycle. No-op when sig_state is empty."""
        if not sig_state:
            return
        # delta[(mk, th)] = sum over severities of cycle_counts[(mk, th, *)].
        # sev_seen[(mk, th)] = the distinct severities observed for that signature
        # this cycle; _pick_first_seen_severity collapses them to one (highest-rank).
        delta: dict[tuple[str, str], int] = {}
        sev_seen: dict[tuple[str, str], list[str]] = {}
        for (mk, th, sev), c in cycle_counts.items():
            key = (mk, th)
            delta[key] = delta.get(key, 0) + c
            sev_seen.setdefault(key, []).append(sev)
        async with self._repo.transaction() as conn:
            for (model_key, template_hash), (
                _size,
                first_seen_ts,
                template_str,
            ) in sig_state.items():
                first_seen_sev = (
                    _pick_first_seen_severity(sev_seen[(model_key, template_hash)])
                    if sev_seen.get((model_key, template_hash))
                    else None
                )
                await conn.execute(
                    text(
                        "INSERT INTO log_signatures "
                        "  (template_hash, service_key, template_str, label, status, "
                        "   first_seen_at, first_seen_severity, last_seen_at, total_count) "
                        "VALUES "
                        "  (:h, :s, :tstr, NULL, 'active', :first, :fss, :last, :delta) "
                        "ON CONFLICT(template_hash, service_key) DO UPDATE SET "
                        "  last_seen_at = excluded.last_seen_at, "
                        "  total_count = log_signatures.total_count + :delta, "
                        "  template_str = excluded.template_str"
                    ),
                    {
                        "h": template_hash,
                        "s": model_key,
                        "tstr": template_str,
                        "first": first_seen_ts,
                        "fss": first_seen_sev,
                        "last": last_seen_at,
                        "delta": delta.get((model_key, template_hash), 0),
                    },
                )


__all__ = ["SignatureCatalogSync"]
