"""Repository for the log_signatures catalog table (STAGE-004-028).

One row per (template_hash, service_key). `label` + `status` are user-owned and
edited via this repo; the drain sync (signature_sync.py) only ever touches
template_str / last_seen_at / total_count / first_seen_at / first_seen_severity
(the last INSERT-only, preserved on UPDATE, like first_seen_at). All *_at are
unix-ms INT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text

from homelab_monitor.kernel.db.repository import SqliteRepository

SignatureStatus = Literal["active", "suppressed", "expected"]

_SORT_COLUMNS: dict[str, str] = {
    "last_seen_at": "last_seen_at",
    "total_count": "total_count",
    "service_key": "service_key COLLATE NOCASE",
    "template_str": "template_str COLLATE NOCASE",
}
_DEFAULT_SORT = "last_seen_at"


@dataclass(frozen=True, slots=True)
class Signature:
    template_hash: str
    service_key: str
    template_str: str
    label: str | None
    status: str
    first_seen_at: int
    first_seen_severity: str | None
    last_seen_at: int
    total_count: int


@dataclass(frozen=True, slots=True)
class SignatureFilter:
    service: str | None = None
    status: str | None = None
    label_q: str | None = None


class SignaturesRepository:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    async def list(
        self,
        *,
        filter: SignatureFilter,
        limit: int,
        offset: int,
        sort: str | None = None,
        descending: bool = True,
    ) -> tuple[list[Signature], int]:
        where_sql, params = self._build_where(filter)
        sort_col = _SORT_COLUMNS.get(sort or _DEFAULT_SORT, _SORT_COLUMNS[_DEFAULT_SORT])
        direction = "DESC" if descending else "ASC"
        total_rows = await self._repo.fetch_all(
            text(f"SELECT COUNT(*) AS n FROM log_signatures{where_sql}"),
            params,
        )
        total = int(total_rows[0].n)  # pyright: ignore[reportAttributeAccessIssue]
        rows = await self._repo.fetch_all(
            text(
                "SELECT template_hash, service_key, template_str, label, status, "
                "  first_seen_at, first_seen_severity, last_seen_at, total_count "
                f"FROM log_signatures{where_sql} "
                f"ORDER BY {sort_col} {direction}, template_hash ASC "
                "LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
        return [_row_to_signature(r) for r in rows], total

    async def get(self, template_hash: str, service_key: str) -> Signature | None:
        rows = await self._repo.fetch_all(
            text(
                "SELECT template_hash, service_key, template_str, label, status, "
                "  first_seen_at, first_seen_severity, last_seen_at, total_count "
                "FROM log_signatures WHERE template_hash = :h AND service_key = :s"
            ),
            {"h": template_hash, "s": service_key},
        )
        if not rows:
            return None
        return _row_to_signature(rows[0])

    async def update_label(
        self, template_hash: str, service_key: str, label: str | None
    ) -> Signature | None:
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "UPDATE log_signatures SET label = :label "
                    "WHERE template_hash = :h AND service_key = :s"
                ),
                {"label": label, "h": template_hash, "s": service_key},
            )
            if (result.rowcount or 0) == 0:
                return None
        return await self.get(template_hash, service_key)

    async def set_status(
        self, template_hash: str, service_key: str, status: SignatureStatus
    ) -> Signature | None:
        async with self._repo.transaction() as conn:
            result = await conn.execute(
                text(
                    "UPDATE log_signatures SET status = :status "
                    "WHERE template_hash = :h AND service_key = :s"
                ),
                {"status": status, "h": template_hash, "s": service_key},
            )
            if (result.rowcount or 0) == 0:
                return None
        return await self.get(template_hash, service_key)

    @staticmethod
    def _build_where(f: SignatureFilter) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if f.service is not None:
            clauses.append("service_key = :service")
            params["service"] = f.service
        if f.status is not None:
            clauses.append("status = :status")
            params["status"] = f.status
        if f.label_q is not None:
            clauses.append("label LIKE :label_q")
            params["label_q"] = f"%{f.label_q}%"
        if not clauses:
            return "", params
        return " WHERE " + " AND ".join(clauses), params


def _row_to_signature(r: Any) -> Signature:  # noqa: ANN401 -- SQLite Row
    raw_label = r.label  # pyright: ignore[reportAttributeAccessIssue]
    raw_fss = r.first_seen_severity  # pyright: ignore[reportAttributeAccessIssue]
    return Signature(
        template_hash=str(r.template_hash),  # pyright: ignore[reportAttributeAccessIssue]
        service_key=str(r.service_key),  # pyright: ignore[reportAttributeAccessIssue]
        template_str=str(r.template_str),  # pyright: ignore[reportAttributeAccessIssue]
        label=(None if raw_label is None else str(raw_label)),
        status=str(r.status),  # pyright: ignore[reportAttributeAccessIssue]
        first_seen_at=int(r.first_seen_at),  # pyright: ignore[reportAttributeAccessIssue]
        first_seen_severity=(None if raw_fss is None else str(raw_fss)),
        last_seen_at=int(r.last_seen_at),  # pyright: ignore[reportAttributeAccessIssue]
        total_count=int(r.total_count),  # pyright: ignore[reportAttributeAccessIssue]
    )


__all__ = ["Signature", "SignatureFilter", "SignatureStatus", "SignaturesRepository"]
