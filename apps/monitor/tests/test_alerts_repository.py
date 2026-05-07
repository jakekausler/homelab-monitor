"""Tests for kernel.alerts.repository.AlertRepository."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import (
    Alert,
    AlertOutcome,
    AlertStatus,
    Severity,
)
from homelab_monitor.kernel.db.repository import SqliteRepository


def _make_alert(
    *,
    fingerprint: str = "fp-1",
    source_tool: str = "alertmanager",
    severity: Severity = Severity.WARNING,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> tuple[Alert, str]:
    """Build an in-memory ``Alert`` plus its corresponding ``payload_json``."""
    labels = labels if labels is not None else {"alertname": "Foo"}
    annotations = annotations if annotations is not None else {}
    payload = {
        "labels": labels,
        "annotations": annotations,
        "extra": "data",
    }
    payload_json = json.dumps(payload)
    alert = Alert(
        id="placeholder",  # ignored by insert_firing
        fingerprint=fingerprint,
        source_tool=source_tool,
        severity=severity,
        status=AlertStatus.FIRING,
        opened_at="placeholder",
        last_seen_at="placeholder",
        payload=payload,
        labels=labels,
        annotations=annotations,
    )
    return alert, payload_json


@pytest.mark.asyncio
async def test_insert_firing_creates_row_and_audit(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-A")
    new_id = await ar.insert_firing(alert, pj)

    row = await repo.fetch_one(
        text("SELECT id, fingerprint, status FROM alerts WHERE id = :i"),
        {"i": new_id},
    )
    assert row is not None
    assert row[1] == "fp-A"
    assert row[2] == "firing"

    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "alert.fire"},
    )
    assert audit is not None
    assert audit[0] == "alert.fire"


@pytest.mark.asyncio
async def test_insert_firing_derives_payload_json_when_none(repo: SqliteRepository) -> None:
    """When payload_json=None, insert_firing serialises alert.payload internally."""
    ar = AlertRepository(repo)
    alert, _ = _make_alert(fingerprint="fp-default-pj")
    # Pass NO payload_json; expect repo to derive it from alert.payload
    new_id = await ar.insert_firing(alert)

    fetched = await ar.get_alert_by_id(new_id)
    assert fetched is not None
    # Verify payload round-trips through serialization
    assert fetched.payload == alert.payload


@pytest.mark.asyncio
async def test_find_active_by_fingerprint_returns_unresolved(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-B")
    new_id = await ar.insert_firing(alert, pj)
    found = await ar.find_active_by_fingerprint("fp-B")
    assert found is not None
    assert found.id == new_id


@pytest.mark.asyncio
async def test_find_active_by_fingerprint_skips_resolved(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-C")
    new_id = await ar.insert_firing(alert, pj)
    await ar.mark_resolved(new_id, "2026-05-07T01:00:00+00:00")
    found = await ar.find_active_by_fingerprint("fp-C")
    assert found is None


@pytest.mark.asyncio
async def test_update_last_seen_writes_only_that_field(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-D")
    new_id = await ar.insert_firing(alert, pj)
    before = await repo.fetch_one(
        text("SELECT opened_at, last_seen_at FROM alerts WHERE id = :i"), {"i": new_id}
    )
    assert before is not None
    original_opened = before[0]

    new_ts = "2026-05-07T02:00:00+00:00"
    await ar.update_last_seen(new_id, new_ts)

    after = await repo.fetch_one(
        text("SELECT opened_at, last_seen_at FROM alerts WHERE id = :i"), {"i": new_id}
    )
    assert after is not None
    assert after[0] == original_opened  # opened_at unchanged
    assert after[1] == new_ts


@pytest.mark.asyncio
async def test_mark_resolved_sets_status_and_resolved_at(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-E")
    new_id = await ar.insert_firing(alert, pj)
    await ar.mark_resolved(new_id, "2026-05-07T03:00:00+00:00")
    row = await repo.fetch_one(
        text("SELECT status, resolved_at FROM alerts WHERE id = :i"),
        {"i": new_id},
    )
    assert row is not None
    assert row[0] == "resolved"
    assert row[1] == "2026-05-07T03:00:00+00:00"


@pytest.mark.asyncio
async def test_mark_resolved_idempotent_does_not_double_audit(repo: SqliteRepository) -> None:
    """Calling mark_resolved twice on the same alert does not double-write audit rows.

    The second call is a no-op (rowcount=0) because the alert is already resolved.
    This tests the coverage branch at repository.py:295 (if result.rowcount > 0).
    """
    ar = AlertRepository(repo)
    alert, pj = _make_alert(fingerprint="fp-idempotent")
    new_id = await ar.insert_firing(alert, pj)

    # First call: marks as resolved, writes audit row
    await ar.mark_resolved(new_id, "2026-05-07T03:00:00+00:00")

    # Count audit rows for this alert
    audit_count_1 = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what = 'alert.resolve' AND after_json LIKE :id"),
        {"id": f'%"{new_id}"%'},
    )
    assert audit_count_1 is not None
    assert audit_count_1[0] == 1

    # Second call: no-op, should NOT write another audit row
    await ar.mark_resolved(new_id, "2026-05-07T04:00:00+00:00")

    audit_count_2 = await repo.fetch_one(
        text("SELECT COUNT(*) FROM audit_log WHERE what = 'alert.resolve' AND after_json LIKE :id"),
        {"id": f'%"{new_id}"%'},
    )
    assert audit_count_2 is not None
    assert audit_count_2[0] == 1  # Still 1, not 2

    # Verify the row was not modified by the no-op second call
    row = await repo.fetch_one(
        text("SELECT status, resolved_at FROM alerts WHERE id = :i"),
        {"i": new_id},
    )
    assert row is not None
    assert row[0] == "resolved"
    assert row[1] == "2026-05-07T03:00:00+00:00"  # First call's timestamp


@pytest.mark.asyncio
async def test_list_alerts_filter_by_status(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a1, pj1 = _make_alert(fingerprint="fp-F1")
    a2, pj2 = _make_alert(fingerprint="fp-F2")
    id1 = await ar.insert_firing(a1, pj1)
    await ar.insert_firing(a2, pj2)
    await ar.mark_resolved(id1, "2026-05-07T04:00:00+00:00")

    firing, _ = await ar.list_alerts(status=AlertStatus.FIRING)
    resolved, _ = await ar.list_alerts(status=AlertStatus.RESOLVED)
    assert {a.fingerprint for a in firing} == {"fp-F2"}
    assert {a.fingerprint for a in resolved} == {"fp-F1"}


@pytest.mark.asyncio
async def test_list_alerts_filter_by_severity(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a1, pj1 = _make_alert(fingerprint="fp-G1", severity=Severity.WARNING)
    a2, pj2 = _make_alert(fingerprint="fp-G2", severity=Severity.CRITICAL)
    await ar.insert_firing(a1, pj1)
    await ar.insert_firing(a2, pj2)

    crit, _ = await ar.list_alerts(severity=Severity.CRITICAL)
    assert {a.fingerprint for a in crit} == {"fp-G2"}


@pytest.mark.asyncio
async def test_list_alerts_filter_by_source_tool(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a1, pj1 = _make_alert(fingerprint="fp-H1", source_tool="alertmanager")
    a2, pj2 = _make_alert(fingerprint="fp-H2", source_tool="netdata")
    await ar.insert_firing(a1, pj1)
    await ar.insert_firing(a2, pj2)

    nd, _ = await ar.list_alerts(source_tool="netdata")
    assert {a.fingerprint for a in nd} == {"fp-H2"}


@pytest.mark.asyncio
async def test_list_alerts_filter_by_fingerprint(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a1, pj1 = _make_alert(fingerprint="fp-I1")
    a2, pj2 = _make_alert(fingerprint="fp-I2")
    await ar.insert_firing(a1, pj1)
    await ar.insert_firing(a2, pj2)

    only, _ = await ar.list_alerts(fingerprint="fp-I1")
    assert {a.fingerprint for a in only} == {"fp-I1"}


@pytest.mark.asyncio
async def test_list_alerts_pagination_cursor(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    for n in range(3):
        a, pj = _make_alert(fingerprint=f"fp-J{n}")
        await ar.insert_firing(a, pj)

    page1, cursor = await ar.list_alerts(limit=2)
    assert len(page1) == 2  # noqa: PLR2004
    assert cursor is not None

    page2, cursor2 = await ar.list_alerts(limit=2, cursor=cursor)
    assert len(page2) == 1
    assert cursor2 is None  # final page

    # Combined set covers all three
    all_fps = {a.fingerprint for a in (*page1, *page2)}
    assert all_fps == {"fp-J0", "fp-J1", "fp-J2"}


@pytest.mark.asyncio
async def test_get_alert_by_id_hydrates_payload(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a, pj = _make_alert(
        fingerprint="fp-K",
        labels={"alertname": "X", "severity": "warning"},
        annotations={"summary": "hi"},
    )
    new_id = await ar.insert_firing(a, pj)

    fetched = await ar.get_alert_by_id(new_id)
    assert fetched is not None
    assert fetched.payload["extra"] == "data"
    assert fetched.labels == {"alertname": "X", "severity": "warning"}
    assert fetched.annotations == {"summary": "hi"}


@pytest.mark.asyncio
async def test_insert_outcome_creates_row_and_audit(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a, pj = _make_alert(fingerprint="fp-L")
    alert_id = await ar.insert_firing(a, pj)

    outcome_id = await ar.insert_outcome(alert_id, AlertOutcome.ACKED, decided_by=None)
    row = await repo.fetch_one(
        text("SELECT outcome, alert_id FROM alert_outcomes WHERE id = :i"), {"i": outcome_id}
    )
    assert row is not None
    assert row[0] == "acked"
    assert row[1] == alert_id

    audit = await repo.fetch_one(
        text("SELECT what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "alert.outcome.acked"},
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_list_outcomes_returns_descending(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    a, pj = _make_alert(fingerprint="fp-M")
    alert_id = await ar.insert_firing(a, pj)

    await ar.insert_outcome(alert_id, AlertOutcome.ACKED, decided_by=None)
    await ar.insert_outcome(alert_id, AlertOutcome.AUTO_FIXED, decided_by=None)

    outcomes = await ar.list_outcomes(alert_id)
    assert len(outcomes) == 2  # noqa: PLR2004
    # Descending by decided_at: most recent (auto_fixed) first
    assert outcomes[0]["outcome"] == "auto_fixed"
    assert outcomes[1]["outcome"] == "acked"


@pytest.mark.asyncio
async def test_get_alert_by_id_returns_none_for_missing(repo: SqliteRepository) -> None:
    ar = AlertRepository(repo)
    result = await ar.get_alert_by_id("nonexistent-id")
    assert result is None


# ----- Spec B additions -----


@pytest.mark.asyncio
async def test_set_ack_updates_columns_and_writes_audit(repo: SqliteRepository) -> None:
    from homelab_monitor.kernel.auth.passwords import hash_password  # noqa: PLC0415
    from homelab_monitor.kernel.auth.repository import AuthRepository  # noqa: PLC0415

    auth_repo = AuthRepository(repo)
    user = await auth_repo.create_user("acker", hash_password("password1234", cost=4))

    ar = AlertRepository(repo)
    a, pj = _make_alert(fingerprint="fp-ack-1")
    alert_id = await ar.insert_firing(a, pj)

    ack_ts = "2026-05-07T10:00:00+00:00"
    await ar.set_ack(alert_id, ack_at=ack_ts, ack_by=user.id)

    row = await repo.fetch_one(
        text("SELECT ack_at, ack_by FROM alerts WHERE id = :i"), {"i": alert_id}
    )
    assert row is not None
    assert row[0] == ack_ts
    assert row[1] == user.id

    audit = await repo.fetch_one(
        text("SELECT who, what FROM audit_log WHERE what = :w ORDER BY id DESC LIMIT 1"),
        {"w": "alert.ack"},
    )
    assert audit is not None
    assert audit[0] == str(user.id)
    assert audit[1] == "alert.ack"


@pytest.mark.asyncio
async def test_find_active_quarantine_alert_matches_on_collector_name(
    repo: SqliteRepository,
) -> None:
    import json as _json  # noqa: PLC0415

    ar = AlertRepository(repo)

    # Insert two scheduler-sourced alerts with different collector_name in payload
    for name in ("coll-alpha", "coll-beta"):
        payload = {
            "labels": {"alertname": "collector_quarantined", "collector_name": name},
            "annotations": {},
            "collector_name": name,
            "reason": "boom",
            "consecutive_failures": 5,
        }
        pj = _json.dumps(payload)
        alert = Alert(
            id="placeholder",
            fingerprint=f"q-fp-{name}",
            source_tool="scheduler",
            severity=Severity.WARNING,
            status=AlertStatus.FIRING,
            opened_at="placeholder",
            last_seen_at="placeholder",
            payload=payload,
            labels=payload["labels"],  # type: ignore[arg-type]
            annotations={},
        )
        await ar.insert_firing(alert, pj)

    found = await ar.find_active_quarantine_alert("coll-alpha")
    assert found is not None
    assert found.labels.get("collector_name") == "coll-alpha"

    found_beta = await ar.find_active_quarantine_alert("coll-beta")
    assert found_beta is not None
    assert found_beta.labels.get("collector_name") == "coll-beta"


@pytest.mark.asyncio
async def test_find_active_quarantine_alert_skips_resolved(
    repo: SqliteRepository,
) -> None:
    import json as _json  # noqa: PLC0415

    ar = AlertRepository(repo)
    payload = {
        "labels": {"alertname": "collector_quarantined", "collector_name": "coll-gone"},
        "annotations": {},
        "collector_name": "coll-gone",
        "reason": "timeout",
        "consecutive_failures": 5,
    }
    pj = _json.dumps(payload)
    alert = Alert(
        id="placeholder",
        fingerprint="q-fp-gone",
        source_tool="scheduler",
        severity=Severity.WARNING,
        status=AlertStatus.FIRING,
        opened_at="placeholder",
        last_seen_at="placeholder",
        payload=payload,
        labels=payload["labels"],  # type: ignore[arg-type]
        annotations={},
    )
    alert_id = await ar.insert_firing(alert, pj)
    await ar.mark_resolved(alert_id, "2026-05-07T11:00:00+00:00")

    result = await ar.find_active_quarantine_alert("coll-gone")
    assert result is None


@pytest.mark.asyncio
async def test_find_active_quarantine_alert_skips_other_source_tools(
    repo: SqliteRepository,
) -> None:
    ar = AlertRepository(repo)
    # Insert a firing alert with source_tool="alertmanager" — should NOT match
    a, pj = _make_alert(
        fingerprint="q-fp-other-src",
        source_tool="alertmanager",
        labels={"alertname": "collector_quarantined", "collector_name": "coll-x"},
    )
    await ar.insert_firing(a, pj)

    result = await ar.find_active_quarantine_alert("coll-x")
    assert result is None


def test_cannot_deserialize_invalid_severity() -> None:
    """Constructing an Alert with an invalid severity raises ValidationError."""
    from pydantic import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError):
        Alert(
            id="abc",
            fingerprint="fp",
            source_tool="x",
            severity="invalid_severity",  # type: ignore[arg-type]
            status=AlertStatus.FIRING,
            opened_at="2026-05-07T00:00:00+00:00",
            last_seen_at="2026-05-07T00:00:00+00:00",
            payload={},
            labels={},
            annotations={},
        )


@pytest.mark.asyncio
async def test_list_alerts_malformed_cursor_raises_value_error(
    repo: SqliteRepository,
) -> None:
    """F16: a cursor missing the ``|`` separator (or with empty halves) raises ValueError.

    The route handler is responsible for mapping ``ValueError`` to HTTP 400;
    this test pins the repository contract.
    """
    ar = AlertRepository(repo)

    for bad in ("not-a-cursor", "|", "abc|", "|xyz"):
        with pytest.raises(ValueError, match="invalid cursor format"):
            await ar.list_alerts(cursor=bad)
