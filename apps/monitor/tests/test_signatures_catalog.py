"""Tests for the signature catalog API endpoints (STAGE-004-028).

Covers:
  GET  /api/logs/signatures              (list + filters)
  GET  /api/logs/signatures/{h}/{s}      (get one)
  PATCH /api/logs/signatures/{h}/{s}     (patch label/status)
  GET  /api/logs/signatures/{h}/{s}/samples (live VL samples)
  _signature_samples_expr unit tests
  Route-order regression (refresh endpoints not swallowed by {template_hash})

Project test conventions:
- Framework: pytest-asyncio (asyncio_mode=auto)
- CSRF: _csrf() reads homelab_monitor_csrf cookie → X-CSRF-Token header
- VL mock: pytest_httpx HTTPXMock with url=re.compile(r"http://.*:9428/select/logsql/query.*")
- Repo seeding: direct INSERT via SignaturesRepository.update_label / SignatureCatalogSync
"""

from __future__ import annotations

import json
import re
from typing import cast

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock
from sqlalchemy import text

from homelab_monitor.kernel.api.routers.logs import (
    _signature_samples_expr,  # pyright: ignore[reportPrivateUsage]
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.signatures_repo import SignaturesRepository

_LIST_URL = "/api/logs/signatures"
_VL_QUERY_RE = re.compile(r"http://.*:9428/select/logsql/query.*")


# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------


def _csrf(client: AsyncClient) -> dict[str, str]:
    """Return X-CSRF-Token header extracted from the session cookie."""
    csrf: str = client.cookies.get("homelab_monitor_csrf") or ""
    return {"X-CSRF-Token": csrf}


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _insert_sig(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    template_hash: str,
    service_key: str,
    template_str: str = "foo <*> bar",
    label: str | None = None,
    status: str = "active",
    first_seen_at: int = 1000,
    last_seen_at: int = 2000,
    total_count: int = 5,
) -> None:
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO log_signatures "
                "  (template_hash, service_key, template_str, label, status, "
                "   first_seen_at, last_seen_at, total_count) "
                "VALUES "
                "  (:h, :s, :tstr, :label, :status, :first, :last, :cnt)"
            ),
            {
                "h": template_hash,
                "s": service_key,
                "tstr": template_str,
                "label": label,
                "status": status,
                "first": first_seen_at,
                "last": last_seen_at,
                "cnt": total_count,
            },
        )


def _get_app_repo(client: AsyncClient) -> SqliteRepository:
    """Extract the per-test SqliteRepository from the shared app state."""
    app = cast(FastAPI, client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    return cast(SqliteRepository, app.state.repo)  # pyright: ignore[reportAttributeAccessIssue]


# ===========================================================================
# GET /api/logs/signatures — list
# ===========================================================================


async def test_list_signatures_200_returns_all(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/signatures returns 200 with list + total."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svcA")
    await _insert_sig(repo, template_hash="h2", service_key="svcB")

    resp = await authenticated_client.get(_LIST_URL)
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 2  # noqa: PLR2004
    hashes = {s["template_hash"] for s in body["signatures"]}
    assert hashes == {"h1", "h2"}


async def test_list_signatures_filter_by_service(authenticated_client: AsyncClient) -> None:
    """GET ?service=svcA filters to rows for that service only."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svcA")
    await _insert_sig(repo, template_hash="h2", service_key="svcB")

    resp = await authenticated_client.get(_LIST_URL, params={"service": "svcA"})
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 1
    assert body["signatures"][0]["service_key"] == "svcA"


async def test_list_signatures_filter_by_status(authenticated_client: AsyncClient) -> None:
    """GET ?status=suppressed filters by status."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", status="active")
    await _insert_sig(repo, template_hash="h2", service_key="svc", status="suppressed")

    resp = await authenticated_client.get(_LIST_URL, params={"status": "suppressed"})
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 1
    assert body["signatures"][0]["status"] == "suppressed"


async def test_list_signatures_filter_by_label_q(authenticated_client: AsyncClient) -> None:
    """GET ?label_q=mine filters by label substring."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label="mine-label")
    await _insert_sig(repo, template_hash="h2", service_key="svc", label="other")

    resp = await authenticated_client.get(_LIST_URL, params={"label_q": "mine"})
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 1
    assert body["signatures"][0]["template_hash"] == "h1"


async def test_list_signatures_pagination(authenticated_client: AsyncClient) -> None:
    """GET ?limit=2&offset=0 paginates; total reflects full count."""
    repo = _get_app_repo(authenticated_client)
    for i in range(4):
        await _insert_sig(repo, template_hash=f"h{i}", service_key="svc", last_seen_at=1000 + i)

    resp = await authenticated_client.get(_LIST_URL, params={"limit": 2, "offset": 0})
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["total"] == 4  # noqa: PLR2004 -- full count
    assert len(body["signatures"]) == 2  # noqa: PLR2004


async def test_list_signatures_401_without_session(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/signatures 401 without session."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get(_LIST_URL)
    assert resp.status_code == 401  # noqa: PLR2004


# ===========================================================================
# GET /api/logs/signatures/{h}/{s} — get one
# ===========================================================================


async def test_get_signature_200_hit(authenticated_client: AsyncClient) -> None:
    """GET /{h}/{s} returns 200 with full signature fields on a hit."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(
        repo,
        template_hash="abc123",
        service_key="svcA",
        template_str="error connecting to <*>",
        label="net-errors",
        status="suppressed",
        first_seen_at=500,
        last_seen_at=9999,
        total_count=42,
    )

    resp = await authenticated_client.get(f"{_LIST_URL}/abc123/svcA")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["template_hash"] == "abc123"
    assert body["service_key"] == "svcA"
    assert body["template_str"] == "error connecting to <*>"
    assert body["label"] == "net-errors"
    assert body["status"] == "suppressed"
    assert body["first_seen_at"] == 500  # noqa: PLR2004
    assert body["last_seen_at"] == 9999  # noqa: PLR2004
    assert body["total_count"] == 42  # noqa: PLR2004


async def test_get_signature_404_miss(authenticated_client: AsyncClient) -> None:
    """GET /{h}/{s} returns 404 with code='not_found' when the key is absent."""
    resp = await authenticated_client.get(f"{_LIST_URL}/nonexistent/svcX")
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "not_found"


# ===========================================================================
# Route-order regression: refresh endpoints not swallowed by {template_hash}
# ===========================================================================


async def test_post_refresh_still_reachable(authenticated_client: AsyncClient) -> None:
    """POST /api/logs/signatures/refresh returns 503 (no consumer), not 404/422 from {h}/{s}."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.post(f"{_LIST_URL}/refresh", headers=csrf)
    # 503 drain_unavailable = endpoint was reached (drain_consumer is None in tests)
    assert resp.status_code == 503  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "drain_unavailable"


async def test_get_refresh_cycle_status_still_reachable(authenticated_client: AsyncClient) -> None:
    """GET /api/logs/signatures/refresh/{cycle_id} resolves to 404 for unknown cycle."""
    resp = await authenticated_client.get(f"{_LIST_URL}/refresh/unknown-cycle-id")
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "not_found"


# ===========================================================================
# PATCH /api/logs/signatures/{h}/{s} — update label and/or status
# ===========================================================================


async def test_patch_label_only(authenticated_client: AsyncClient) -> None:
    """PATCH with label-only sets the label and returns updated row."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label=None)

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"label": "new-label"},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["label"] == "new-label"
    assert body["status"] == "active"  # unchanged

    # Verify persisted
    sig_repo = SignaturesRepository(repo)
    row = await sig_repo.get("h1", "svc")
    assert row is not None
    assert row.label == "new-label"


async def test_patch_status_only(authenticated_client: AsyncClient) -> None:
    """PATCH with status-only sets the status and returns updated row."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", status="active")

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"status": "suppressed"},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["status"] == "suppressed"


async def test_patch_label_and_status_together(authenticated_client: AsyncClient) -> None:
    """PATCH with both label and status updates both fields."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc")

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"label": "both-set", "status": "expected"},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["label"] == "both-set"
    assert body["status"] == "expected"


async def test_patch_404_missing_row(authenticated_client: AsyncClient) -> None:
    """PATCH on nonexistent key returns 404."""
    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/nosuchkey/svc",
        json={"status": "suppressed"},
        headers=csrf,
    )
    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["error"]["code"] == "not_found"


async def test_patch_requires_csrf(authenticated_client: AsyncClient) -> None:
    """PATCH without X-CSRF-Token header returns 403."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc")

    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"status": "suppressed"},
        # No CSRF header
    )
    assert resp.status_code == 403  # noqa: PLR2004


async def test_patch_requires_session(authenticated_client: AsyncClient) -> None:
    """PATCH without session cookie returns 401."""
    app = cast(FastAPI, authenticated_client._transport.app)  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage, reportUnknownMemberType]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.patch(
            f"{_LIST_URL}/h1/svc",
            json={"status": "suppressed"},
        )
    assert resp.status_code == 401  # noqa: PLR2004


async def test_patch_status_only_does_not_clear_existing_label(
    authenticated_client: AsyncClient,
) -> None:
    """PATCH with status only (label omitted) leaves an existing label intact."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label="keep-me")

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"status": "expected"},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["label"] == "keep-me"  # model_fields_set contract: label not in set → no update

    sig_repo = SignaturesRepository(repo)
    row = await sig_repo.get("h1", "svc")
    assert row is not None
    assert row.label == "keep-me"


async def test_patch_label_null_clears_existing_label(
    authenticated_client: AsyncClient,
) -> None:
    """PATCH with label=null clears an existing label (model_fields_set honored)."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svc", label="to-clear")

    csrf = _csrf(authenticated_client)
    resp = await authenticated_client.patch(
        f"{_LIST_URL}/h1/svc",
        json={"label": None},
        headers=csrf,
    )
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["label"] is None

    # Verify persisted
    sig_repo = SignaturesRepository(repo)
    row = await sig_repo.get("h1", "svc")
    assert row is not None
    assert row.label is None


# ===========================================================================
# GET /api/logs/signatures/{h}/{s}/samples — live VL samples
# ===========================================================================


def _vl_ndjson_line(msg: str, ts: str = "2026-06-05T12:00:00.000Z") -> bytes:
    """Build one NDJSON line as VL would return from /select/logsql/query."""
    record = {"_time": ts, "_msg": msg, "_stream_id": "stdout", "service": "svcA"}
    return json.dumps(record).encode() + b"\n"


async def test_samples_returns_lines_from_vl(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """GET .../samples returns lines from VL when VL responds with matching lines."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(
        repo, template_hash="h1", service_key="svcA", template_str="error connecting to <*>"
    )

    httpx_mock.add_response(
        method="GET",
        url=_VL_QUERY_RE,
        status_code=200,
        content=_vl_ndjson_line("error connecting to 10.0.0.1"),
    )

    resp = await authenticated_client.get(f"{_LIST_URL}/h1/svcA/samples")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["reason"] is None
    assert len(body["lines"]) >= 1
    assert body["lines"][0]["message"] == "error connecting to 10.0.0.1"


async def test_samples_generic_template_returns_no_vl_call(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """All-wildcard template -> lines=[], reason='template_too_generic', no VL call."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(repo, template_hash="h1", service_key="svcA", template_str="<*>")

    resp = await authenticated_client.get(f"{_LIST_URL}/h1/svcA/samples")
    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert body["lines"] == []
    assert body["reason"] == "template_too_generic"
    # No VL requests should have been made
    assert httpx_mock.get_requests() == []


async def test_samples_vl_error_returns_vl_unavailable(
    authenticated_client: AsyncClient,
    httpx_mock: HTTPXMock,
) -> None:
    """GET .../samples when VL is down returns lines=[], reason='vl_unavailable', HTTP 200."""
    repo = _get_app_repo(authenticated_client)
    await _insert_sig(
        repo, template_hash="h1", service_key="svcA", template_str="error connecting to <*>"
    )

    httpx_mock.add_response(
        method="GET",
        url=_VL_QUERY_RE,
        status_code=503,
        text="service unavailable",
    )

    resp = await authenticated_client.get(f"{_LIST_URL}/h1/svcA/samples")
    assert resp.status_code == 200  # noqa: PLR2004 -- must NOT be 500
    body = resp.json()
    assert body["lines"] == []
    assert body["reason"] == "vl_unavailable"


async def test_samples_404_for_missing_signature(authenticated_client: AsyncClient) -> None:
    """GET .../samples on nonexistent key returns 404."""
    resp = await authenticated_client.get(f"{_LIST_URL}/nosuchkey/svc/samples")
    assert resp.status_code == 404  # noqa: PLR2004


# ===========================================================================
# Unit tests for _signature_samples_expr
# ===========================================================================


def test_samples_expr_normal_template_adds_service_prefix() -> None:
    """Normal template with real service → service: filter ANDed with segments."""
    expr = _signature_samples_expr("foo <*> bar", "svcA")
    assert expr is not None
    assert "service:" in expr
    assert "svcA" in expr
    # Both segments should appear
    assert "foo" in expr
    assert "bar" in expr


def test_samples_expr_cron_service_no_prefix() -> None:
    """service_key starting with 'cron:' does NOT get a service: prefix."""
    expr = _signature_samples_expr("job started <*>", "cron:backup")
    assert expr is not None
    assert "service:" not in expr
    assert "job started" in expr


def test_samples_expr_unknown_service_no_prefix() -> None:
    """service_key == '_unknown' does NOT get a service: prefix."""
    expr = _signature_samples_expr("connection timeout <*>", "_unknown")
    assert expr is not None
    assert "service:" not in expr


def test_samples_expr_all_wildcard_returns_none() -> None:
    """All-wildcard template ('<*>') → None (too generic)."""
    assert _signature_samples_expr("<*>", "svcA") is None


def test_samples_expr_whitespace_only_segments_returns_none() -> None:
    """Template with only whitespace segments → None."""
    assert _signature_samples_expr("  <*>   <*>  ", "svcA") is None


def test_samples_expr_multi_segment_template() -> None:
    """Multiple non-wildcard segments are ANDed together."""
    expr = _signature_samples_expr("alpha <*> beta <*> gamma", "svcX")
    assert expr is not None
    assert " AND " in expr
    assert "alpha" in expr
    assert "beta" in expr
    assert "gamma" in expr


def test_samples_expr_service_prefix_format() -> None:
    """service: prefix uses logsql_quote_phrase formatting."""
    expr = _signature_samples_expr("error <*>", "my-service")
    assert expr is not None
    # The service segment must come first
    assert expr.startswith("service:")
