"""SuggestionsRepository — anchor + Docker sidecar CRUD.

Patterns mirror TargetsRepository:
  - Anchor `suggestions` row carries id, kind, deduplication_key, state, created_at, updated_at.
  - Sidecar `suggestions_docker` row stores Docker-specific fields keyed by suggestion_id.
  - Static `*_conn` helpers used inside an external `repo.transaction()`.
  - Instance reads for API endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from homelab_monitor.kernel.db.ids import uuid7
from homelab_monitor.kernel.db.repository import SqliteRepository

SuggestionState = Literal["pending", "accepted", "ignored", "container_gone"]
ALLOWED_STATES: Final[frozenset[str]] = frozenset(
    {"pending", "accepted", "ignored", "container_gone"}
)
_CURSOR_PART_COUNT: Final[int] = 2


@dataclass(frozen=True, slots=True)
class DockerSuggestionRow:
    """Joined view: suggestions (anchor) + suggestions_docker (sidecar)."""

    id: str
    kind: str
    deduplication_key: str
    state: str
    created_at: str
    updated_at: str
    container_id: str
    container_name: str
    image_ref: str
    labels: dict[str, str]
    compose_project: str | None
    compose_service: str | None
    compose_file_path: str | None
    detection_reason: str


class SuggestionsRepository:
    """Repository for the `suggestions` anchor + `suggestions_docker` sidecar."""

    def __init__(self, repo: SqliteRepository) -> None:
        self._repo: SqliteRepository = repo

    # ---- Static *_conn helpers usable inside repo.transaction() ----

    @staticmethod
    async def insert_or_update_docker_suggestion_conn(  # noqa: PLR0913
        conn: AsyncConnection,
        *,
        kind: str,
        deduplication_key: str,
        container_id: str,
        container_name: str,
        image_ref: str,
        labels: dict[str, str],
        compose_project: str | None,
        compose_service: str | None,
        compose_file_path: str | None,
        detection_reason: str,
        now: str,
    ) -> str:
        """Insert anchor + sidecar rows (UPSERT) and return the suggestion id.

        Dedup is on (kind, deduplication_key) — repeated calls for the same
        container do NOT create duplicate rows. On conflict, updates
        container_name/image_ref/labels/compose_*/detection_reason +
        updated_at; state is preserved (i.e. an 'ignored' suggestion stays
        'ignored' even if the container reappears in a scan).
        """
        sid = uuid7()
        labels_json = json.dumps(labels, sort_keys=True)

        # Anchor: INSERT-or-UPDATE. On conflict the existing id is reused.
        await conn.execute(
            text(
                "INSERT INTO suggestions "
                "  (id, kind, deduplication_key, state, created_at, updated_at) "
                "VALUES (:id, :kind, :dk, 'pending', :now, :now) "
                "ON CONFLICT(kind, deduplication_key) DO UPDATE SET "
                "  updated_at = excluded.updated_at, "
                "  state = CASE "
                "    WHEN suggestions.state = 'container_gone' THEN 'pending' "
                "    ELSE suggestions.state "
                "  END"
            ),
            {"id": sid, "kind": kind, "dk": deduplication_key, "now": now},
        )

        # Re-read the anchor id (it may be the conflicting row's id, not :sid).
        row = await conn.execute(
            text("SELECT id FROM suggestions WHERE kind = :kind AND deduplication_key = :dk"),
            {"kind": kind, "dk": deduplication_key},
        )
        anchor = row.first()
        if anchor is None:  # pragma: no cover -- defensive; the INSERT above guarantees a row
            msg = (
                f"suggestions anchor missing after upsert (kind={kind!r}, dk={deduplication_key!r})"
            )
            raise RuntimeError(msg)
        anchor_id = str(anchor.id)

        # Sidecar: INSERT-or-UPDATE.
        await conn.execute(
            text(
                "INSERT INTO suggestions_docker "
                "  (suggestion_id, container_id, container_name, image_ref, "
                "   labels_json, compose_project, compose_service, "
                "   compose_file_path, detection_reason) "
                "VALUES (:sid, :cid, :cname, :img, :labels, :cp, :cs, :cfp, :reason) "
                "ON CONFLICT(suggestion_id) DO UPDATE SET "
                "  container_id = excluded.container_id, "
                "  container_name = excluded.container_name, "
                "  image_ref = excluded.image_ref, "
                "  labels_json = excluded.labels_json, "
                "  compose_project = excluded.compose_project, "
                "  compose_service = excluded.compose_service, "
                "  compose_file_path = excluded.compose_file_path, "
                "  detection_reason = excluded.detection_reason"
            ),
            {
                "sid": anchor_id,
                "cid": container_id,
                "cname": container_name,
                "img": image_ref,
                "labels": labels_json,
                "cp": compose_project,
                "cs": compose_service,
                "cfp": compose_file_path,
                "reason": detection_reason,
            },
        )
        return anchor_id

    @staticmethod
    async def mark_resolved_conn(
        conn: AsyncConnection,
        *,
        kind: str,
        kept_dedup_keys: set[str],
        now: str,
    ) -> int:
        """Transition pending suggestions of `kind` whose dedup_key is NOT in
        kept_dedup_keys to state 'container_gone'. Returns affected row count.

        Used by file-override loader to clear stale malformed-file suggestions
        when the operator fixes the YAML and the loader stops re-emitting.
        """
        if not kept_dedup_keys:
            # No keys to keep — mark ALL pending of this kind resolved
            result = await conn.execute(
                text(
                    "UPDATE suggestions SET state='container_gone', updated_at=:now "
                    "WHERE kind=:kind AND state='pending'"
                ),
                {"kind": kind, "now": now},
            )
            return int(result.rowcount or 0)
        placeholders = ", ".join(f":k_{i}" for i in range(len(kept_dedup_keys)))
        params: dict[str, str] = {"kind": kind, "now": now}
        for i, key in enumerate(sorted(kept_dedup_keys)):
            params[f"k_{i}"] = key
        result = await conn.execute(
            text(
                f"UPDATE suggestions SET state='container_gone', updated_at=:now "
                f"WHERE kind=:kind AND state='pending' "
                f"AND deduplication_key NOT IN ({placeholders})"
            ),
            params,
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def mark_container_gone_conn(
        conn: AsyncConnection,
        *,
        container_id: str,
        now: str,
    ) -> int:
        """Transition any pending Docker suggestions for this container to
        state='container_gone'. Returns the number of rows affected.

        Suggestions already in a terminal state ('accepted', 'ignored',
        'container_gone') are untouched.
        """
        result = await conn.execute(
            text(
                "UPDATE suggestions SET state = 'container_gone', updated_at = :now "
                "WHERE state = 'pending' AND id IN ("
                "  SELECT suggestion_id FROM suggestions_docker "
                "  WHERE container_id = :cid"
                ")"
            ),
            {"cid": container_id, "now": now},
        )
        return result.rowcount or 0

    # ---- Instance reads used by the API ----

    async def list_pending_docker_suggestions(
        self,
        *,
        status: str = "pending",
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[DockerSuggestionRow], str | None]:
        """List Docker suggestions ordered by created_at DESC, id DESC.

        Cursor format: f"{created_at}|{id}" of the last row of the previous
        page. Returned next_cursor is non-None only when len(rows) == limit
        (i.e. another page may exist).

        `status` accepts 'pending' | 'accepted' | 'ignored' | 'container_gone' | 'all'.
        """
        clauses: list[str] = [
            "s.kind IN ('docker_container_discovered', 'docker_label_collision',"
            " 'docker_file_override_malformed')"
        ]
        params: dict[str, object] = {"lim": limit}
        if status != "all":
            if status not in ALLOWED_STATES:
                raise ValueError(f"invalid suggestion status: {status!r}")
            clauses.append("s.state = :state")
            params["state"] = status
        if cursor:
            parts = cursor.split("|", 1)
            if len(parts) != _CURSOR_PART_COUNT or not parts[0] or not parts[1]:
                raise ValueError("invalid cursor format")
            cur_created, cur_id = parts
            clauses.append(
                "(s.created_at < :cur_created OR (s.created_at = :cur_created AND s.id < :cur_id))"
            )
            params["cur_created"] = cur_created
            params["cur_id"] = cur_id

        sql = (
            "SELECT s.id AS id, s.kind AS kind, s.deduplication_key AS dk, "
            "  s.state AS state, s.created_at AS created_at, s.updated_at AS updated_at, "
            "  d.container_id AS container_id, d.container_name AS container_name, "
            "  d.image_ref AS image_ref, d.labels_json AS labels_json, "
            "  d.compose_project AS compose_project, d.compose_service AS compose_service, "
            "  d.compose_file_path AS compose_file_path, "
            "  d.detection_reason AS detection_reason "
            "FROM suggestions s JOIN suggestions_docker d ON d.suggestion_id = s.id "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY s.created_at DESC, s.id DESC LIMIT :lim"
        )
        rows = await self._repo.fetch_all(text(sql), params)
        items: list[DockerSuggestionRow] = []
        for r in rows:
            labels_raw: str | None = r.labels_json
            labels: dict[str, str] = json.loads(labels_raw) if labels_raw else {}
            items.append(
                DockerSuggestionRow(
                    id=str(r.id),
                    kind=str(r.kind),
                    deduplication_key=str(r.dk),
                    state=str(r.state),
                    created_at=str(r.created_at),
                    updated_at=str(r.updated_at),
                    container_id=str(r.container_id),
                    container_name=str(r.container_name),
                    image_ref=str(r.image_ref),
                    labels=labels,
                    compose_project=None if r.compose_project is None else str(r.compose_project),
                    compose_service=None if r.compose_service is None else str(r.compose_service),
                    compose_file_path=None
                    if r.compose_file_path is None
                    else str(r.compose_file_path),
                    detection_reason=str(r.detection_reason),
                )
            )

        next_cursor: str | None = None
        if len(items) == limit and items:
            last = items[-1]
            next_cursor = f"{last.created_at}|{last.id}"
        return items, next_cursor


__all__ = [
    "ALLOWED_STATES",
    "DockerSuggestionRow",
    "SuggestionState",
    "SuggestionsRepository",
]
