"""Tests for KarmaOutcomeReconciler (STAGE-010-003 Build).

Project test conventions (see test_container_healthcheck_reconciler.py):
- asyncio_mode=auto — bare async def, no decorator.
- MemoryRetainingMetricsWriter for metric assertions.
- httpx.MockTransport for AM stubbing.
- `# noqa: PLR2004` for magic-number assertions.
- Real in-memory migrated DB via the `repo` fixture.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import text

from homelab_monitor.kernel.alerts.repository import AlertRepository
from homelab_monitor.kernel.alerts.types import AlertOutcome
from homelab_monitor.kernel.analyzer.karma_outcome_reconciler import (
    KarmaOutcomeReconciler,
)
from homelab_monitor.kernel.db.repositories.app_settings_repository import (
    AppSettingsRepository,
)
from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    MemoryRetainingMetricsWriter,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    repo: SqliteRepository,
    http: httpx.AsyncClient,
    vm: MemoryRetainingMetricsWriter | None = None,
) -> CollectorContext:
    """Minimal CollectorContext for the reconciler."""
    return CollectorContext(
        config=CollectorConfig(name="karma_outcome_reconciler"),
        db=repo,
        vm=vm or MemoryRetainingMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=http,
        ssh=None,  # pyright: ignore[reportArgumentType]
        secrets=None,  # pyright: ignore[reportArgumentType]
        log=structlog.get_logger().bind(collector="karma_outcome_reconciler"),  # pyright: ignore[reportArgumentType]
        ha=None,
    )


def _fp(labels: dict[str, str]) -> str:
    """Fingerprint helper mirroring the reconciler's algorithm."""
    return hashlib.sha256(
        json.dumps(labels, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _seed_alert(  # noqa: PLR0913
    repo: SqliteRepository,
    *,
    alert_id: str,
    fingerprint: str,
    status: str = "firing",
    resolved_at: str | None = None,
    opened_at: str = "2026-06-01T12:00:00+00:00",
) -> None:
    """Seed one row into ``alerts`` directly (bypasses AlertRepository)."""
    async with repo.transaction() as conn:
        await conn.execute(
            text(
                "INSERT INTO alerts (id, fingerprint, source_tool, severity, "
                "status, opened_at, last_seen_at, resolved_at, payload_json, "
                "created_at) "
                "VALUES (:id, :fp, 'vmalert-metrics', 'warning', :status, "
                ":opened, :opened, :resolved, '{}', :opened)"
            ),
            {
                "id": alert_id,
                "fp": fingerprint,
                "status": status,
                "opened": opened_at,
                "resolved": resolved_at,
            },
        )


def _make_mock_transport(
    silences: list[dict[str, Any]] | None = None,
    *,
    status_code: int = 200,
) -> httpx.MockTransport:
    """Build a MockTransport that serves ``/api/v2/silences``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/silences":
            if status_code != 200:  # noqa: PLR2004
                return httpx.Response(status_code)
            return httpx.Response(200, json=silences or [])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _silence(
    *,
    silence_id: str,
    matchers: list[dict[str, Any]],
    starts_at: str = "2026-06-01T12:00:00Z",
    state: str = "active",
) -> dict[str, Any]:
    """Build one AM silence JSON object matching the v2 shape."""
    return {
        "id": silence_id,
        "status": {"state": state},
        "startsAt": starts_at,
        "endsAt": "2027-06-01T12:00:00Z",
        "matchers": matchers,
    }


def _fp_matcher(value: str) -> dict[str, Any]:
    return {"name": "fingerprint", "value": value, "isRegex": False, "isEqual": True}


def _eq_matcher(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value, "isRegex": False, "isEqual": True}


def _regex_matcher(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value, "isRegex": True, "isEqual": True}


# ---------------------------------------------------------------------------
# SILENCE-path tests
# ---------------------------------------------------------------------------


async def test_silence_with_fingerprint_matcher_writes_acked(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with single ``name=fingerprint`` matcher → ACKED row written."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    fp = "abc123"
    await _seed_alert(repo, alert_id="a1", fingerprint=fp)

    silence = _silence(
        silence_id="s1",
        matchers=[_fp_matcher(fp)],
        starts_at="2026-06-01T12:00:00Z",
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True

    outcomes = await AlertRepository(repo).list_outcomes("a1")
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == AlertOutcome.ACKED.value
    assert outcomes[0]["decided_by"] == "karma"

    matched = [
        e for e in vm.recorded if e.name == "homelab_karma_reconciler_silences_matched_total"
    ]
    assert matched[-1].value == 1.0


async def test_silence_with_label_matchers_single_match_writes_acked(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Label matchers whose reconstructed fingerprint matches exactly one alert → ACKED."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    labels = {"alertname": "DiskFull", "severity": "warning"}
    fp = _fp(labels)
    await _seed_alert(repo, alert_id="a1", fingerprint=fp)

    silence = _silence(
        silence_id="s1",
        matchers=[_eq_matcher("alertname", "DiskFull"), _eq_matcher("severity", "warning")],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == AlertOutcome.ACKED.value


async def test_silence_with_label_matchers_multi_match_is_skipped(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence whose fingerprint matches >1 alerts → skipped, ambiguous_match counter++."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    labels = {"alertname": "DiskFull"}
    fp = _fp(labels)
    # Seed two rows sharing the same fingerprint — one firing, one resolved
    # (both are allowed by the schema; the UNIQUE partial index only fires on
    # ``status='firing'`` so this seeding is valid).
    await _seed_alert(repo, alert_id="a1", fingerprint=fp, status="firing")
    await _seed_alert(
        repo,
        alert_id="a2",
        fingerprint=fp,
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )

    silence = _silence(silence_id="s1", matchers=[_eq_matcher("alertname", "DiskFull")])
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    # Neither alert got an ACKED row.
    for aid in ("a1", "a2"):
        outcomes_for = [
            o
            for o in await AlertRepository(repo).list_outcomes(aid)
            if o["outcome"] == AlertOutcome.ACKED.value
        ]
        assert outcomes_for == []

    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "ambiguous_match"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_silence_with_label_matchers_no_match_is_skipped_silently(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with fingerprint matching zero rows → skipped, no_match counter++."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(
        silence_id="s1",
        matchers=[_eq_matcher("alertname", "NeverExisted")],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "no_match"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_unsupported_matcher_is_skipped(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with a regex matcher → skipped with unsupported_matcher reason."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(
        silence_id="s1",
        matchers=[_regex_matcher("alertname", "Disk.*")],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "unsupported_matcher"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_pending_silence_is_ignored(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with state='pending' → silently ignored (no matched/skipped counter)."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(
        silence_id="s1",
        matchers=[_eq_matcher("alertname", "DiskFull")],
        state="pending",
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e for e in vm.recorded if e.name == "homelab_karma_reconciler_silences_skipped_total"
    ]
    assert skipped == []


async def test_empty_matchers_is_skipped_unsupported(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with empty matchers list → unsupported_matcher skip."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(silence_id="s1", matchers=[])
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "unsupported_matcher"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_silence_missing_startsAt_is_skipped_invalid_startsAt(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matched silence with missing/empty startsAt → invalid_startsAt skip.

    Renamed from _skipped_unsupported (STAGE-010-003 Finding 3): the missing-
    startsAt case now emits a distinct reason label so it can be alerted on
    separately from unsupported-matcher cases.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    fp = _fp({"alertname": "DiskFull"})
    await _seed_alert(repo, alert_id="a1", fingerprint=fp)

    silence: dict[str, Any] = {
        "id": "s1",
        "status": {"state": "active"},
        "startsAt": "",
        "endsAt": "2027-06-01T12:00:00Z",
        "matchers": [_eq_matcher("alertname", "DiskFull")],
    }
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    acked = [o for o in outcomes if o["outcome"] == AlertOutcome.ACKED.value]
    assert acked == []
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "invalid_startsAt"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_negated_equality_matcher_is_skipped_unsupported(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with isEqual=False matcher → unsupported_matcher skip.

    Also exercises the negative branch of _extract_fingerprint_matcher
    (matcher name != 'fingerprint').
    """
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(
        silence_id="s1",
        matchers=[
            {"name": "alertname", "value": "DiskFull", "isRegex": False, "isEqual": False},
        ],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "unsupported_matcher"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_fingerprint_matcher_with_empty_value_falls_through_to_label_path(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single fingerprint matcher whose value is empty must NOT short-circuit
    — the empty-value check in ``_extract_fingerprint_matcher`` returns None,
    and the code falls through to the label-reconstruction path.

    STAGE-010-003 Finding 2: the fingerprint short-circuit now requires
    EXACTLY one matcher, and it must have a non-empty string value. This test
    exercises the empty-string branch of the value check, forcing the label
    path. The reconstructed label dict is ``{"fingerprint": ""}``; its hash
    is used as the target fingerprint.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    # Seed an alert whose fingerprint is the SHA-256 of {"fingerprint": ""}
    # so the label-reconstruction path matches it exactly.
    fp = _fp({"fingerprint": ""})
    await _seed_alert(repo, alert_id="a1", fingerprint=fp)

    silence = _silence(
        silence_id="s1",
        matchers=[
            {"name": "fingerprint", "value": "", "isRegex": False, "isEqual": True},
        ],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == AlertOutcome.ACKED.value


async def test_matcher_with_non_string_value_is_skipped_unsupported(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with non-string matcher value → unsupported_matcher skip."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence: dict[str, Any] = {
        "id": "s1",
        "status": {"state": "active"},
        "startsAt": "2026-06-01T12:00:00Z",
        "endsAt": "2027-06-01T12:00:00Z",
        "matchers": [
            {"name": "alertname", "value": 42, "isRegex": False, "isEqual": True},
        ],
    }
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "unsupported_matcher"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_corrupted_last_reconciliation_timestamp_falls_back_to_zero_lag(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-seeded garbage last_reconciliation_at → ValueError caught, lag=0.0."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    await AppSettingsRepository(repo).set("analyzer.last_reconciliation_at", "not-a-timestamp")

    transport = _make_mock_transport([])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    lag = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_last_reconciliation_lag_seconds"
    ]
    assert lag and lag[-1].value == 0.0


# ---------------------------------------------------------------------------
# RESOLUTION-path tests
# ---------------------------------------------------------------------------


async def test_alert_resolved_between_ticks_writes_auto_resolved(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alert resolved after last_reconciliation_at → AUTO_RESOLVED row written."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    # Seed one resolved alert. First-ever run (last_iso is None) picks it up.
    await _seed_alert(
        repo,
        alert_id="a1",
        fingerprint="fp1",
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )

    transport = _make_mock_transport([])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == AlertOutcome.AUTO_RESOLVED.value
    assert outcomes[0]["decided_by"] == "reconciler"

    matched = [
        e for e in vm.recorded if e.name == "homelab_karma_reconciler_resolutions_matched_total"
    ]
    assert matched[-1].value == 1.0


async def test_rerun_with_same_state_is_idempotent(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the reconciler twice with identical state produces no new rows on run 2."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    fp = _fp({"alertname": "DiskFull"})
    await _seed_alert(
        repo,
        alert_id="a1",
        fingerprint=fp,
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )
    silence = _silence(silence_id="s1", matchers=[_eq_matcher("alertname", "DiskFull")])
    transport = _make_mock_transport([silence])

    async with httpx.AsyncClient(transport=transport) as http:
        result1 = await KarmaOutcomeReconciler().run(_ctx(repo, http))
        result2 = await KarmaOutcomeReconciler().run(_ctx(repo, http))

    assert result1.ok is True
    assert result2.ok is True

    outcomes = await AlertRepository(repo).list_outcomes("a1")
    # Exactly two outcome rows exist total (ACKED + AUTO_RESOLVED), NOT four.
    assert len(outcomes) == 2  # noqa: PLR2004
    kinds = {o["outcome"] for o in outcomes}
    assert kinds == {AlertOutcome.ACKED.value, AlertOutcome.AUTO_RESOLVED.value}

    # Audit rows: exactly one per outcome (the True branch of insert_outcome_if_absent).
    # The False branch (2nd tick) must have written zero audit_log rows for these outcomes.
    audit_rows = await repo.fetch_all(
        text(
            "SELECT COUNT(*) AS c FROM audit_log "
            "WHERE what IN ('alert.outcome.acked', 'alert.outcome.auto_resolved')"
        ),
        {},
    )
    assert int(audit_rows[0].c) == 2  # noqa: PLR2004


async def test_resolution_already_present_is_silent(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved alert already having AUTO_RESOLVED → wrote=False; no double-write."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    await _seed_alert(
        repo,
        alert_id="a1",
        fingerprint="fp1",
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )
    await AlertRepository(repo).insert_outcome_if_absent(
        alert_id="a1",
        outcome=AlertOutcome.AUTO_RESOLVED,
        decided_by="reconciler",
        decided_at="2026-06-01T13:00:00+00:00",
    )

    transport = _make_mock_transport([])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    auto_resolved = [o for o in outcomes if o["outcome"] == AlertOutcome.AUTO_RESOLVED.value]
    assert len(auto_resolved) == 1

    matched = [
        e for e in vm.recorded if e.name == "homelab_karma_reconciler_resolutions_matched_total"
    ]
    assert matched and matched[-1].value == 0.0


# ---------------------------------------------------------------------------
# AM-unavailable path
# ---------------------------------------------------------------------------


async def test_alertmanager_unavailable_returns_error_no_partial_writes(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AM returning 503 → CollectorResult.ok False, no outcome rows, no lag update."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    # Seed a resolved alert — resolution phase MUST NOT run when AM fails.
    await _seed_alert(
        repo,
        alert_id="a1",
        fingerprint="fp1",
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )

    transport = _make_mock_transport(status_code=503)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert result.errors  # non-empty

    outcomes = await AlertRepository(repo).list_outcomes("a1")
    assert outcomes == []

    # last_reconciliation_at MUST NOT be advanced on failure.
    stored = await AppSettingsRepository(repo).get("analyzer.last_reconciliation_at")
    assert stored is None


async def test_alertmanager_network_error_returns_error(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MockTransport raising httpx.ConnectError → CollectorResult.ok False."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert result.errors


async def test_alertmanager_non_list_response_returns_error(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AM returning a JSON object (not list) → treated as failure."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http))

    assert result.ok is False
    assert result.errors


async def test_list_with_non_dict_items_are_filtered(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AM list containing non-dict items → filtered out by SilencesClient."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence = _silence(silence_id="s1", matchers=[_eq_matcher("alertname", "X")])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[silence, "garbage", None])

    transport = httpx.MockTransport(handler)
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    # No alert seeded → surviving silence produces a "no_match" skip.
    assert result.ok is True
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "no_match"
    ]
    assert skipped and skipped[-1].value == 1.0


# ---------------------------------------------------------------------------
# STAGE-010-003 code-review findings tests
# ---------------------------------------------------------------------------


async def test_fingerprint_matcher_alone_is_short_circuit_but_combined_is_label_path(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence with [fingerprint=X, severity=critical] uses label-path,
    not fingerprint short-circuit (STAGE-010-003 Finding 2 tightening)."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    # Seed an alert whose real fingerprint is 'fp1'.
    await _seed_alert(repo, alert_id="a1", fingerprint="fp1")

    # Silence has fingerprint=fp1 AND severity=critical → label-reconstruction path.
    # The reconstructed label set is {"fingerprint": "fp1", "severity": "critical"},
    # whose compute_fingerprint() will NOT equal "fp1" (which is a raw alert
    # fingerprint, not a hash of these labels). Therefore no match → no_match skip.
    silence = _silence(
        silence_id="s1",
        matchers=[
            _eq_matcher("fingerprint", "fp1"),
            _eq_matcher("severity", "critical"),
        ],
    )
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    # No ACKED written (label path did NOT resolve to alert a1).
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    acked = [o for o in outcomes if o["outcome"] == AlertOutcome.ACKED.value]
    assert acked == []
    # Skipped as no_match (not unsupported_matcher — matchers were valid).
    skipped = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_silences_skipped_total"
        and e.labels.get("reason") == "no_match"
    ]
    assert skipped and skipped[-1].value == 1.0


async def test_silence_updatedAt_is_used_as_decided_at_when_present(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AM silence carries updatedAt, that's the decided_at (Finding 4)."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    fp = _fp({"alertname": "DiskFull"})
    await _seed_alert(repo, alert_id="a1", fingerprint=fp)

    silence: dict[str, Any] = {
        "id": "s1",
        "status": {"state": "active"},
        "startsAt": "2026-06-01T12:00:00Z",
        "updatedAt": "2026-06-01T13:30:00Z",
        "endsAt": "2027-06-01T12:00:00Z",
        "matchers": [_eq_matcher("alertname", "DiskFull")],
    }
    transport = _make_mock_transport([silence])
    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    acked = [o for o in outcomes if o["outcome"] == AlertOutcome.ACKED.value]
    assert len(acked) == 1
    assert acked[0]["decided_at"].startswith("2026-06-01T13:30:00")


async def test_bounded_scan_excludes_resolutions_before_last_iso(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WHERE resolved_at > :last` excludes resolutions before the last tick.

    Pins the bounded-scan behavior independently of UNIQUE-index idempotency
    (Finding 6). A regression that swapped `>` for `<` (or dropped the bound)
    would be caught here — the existing rerun test succeeds even if the bound
    is broken because the UNIQUE index absorbs the duplicate write.
    """
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    # Seed a resolved alert at T1.
    await _seed_alert(
        repo,
        alert_id="a1",
        fingerprint="fp1",
        status="resolved",
        resolved_at="2026-06-01T13:00:00+00:00",
    )
    # Set last_reconciliation_at to T2 > T1 — should EXCLUDE the alert.
    await AppSettingsRepository(repo).set(
        "analyzer.last_reconciliation_at", "2026-06-01T14:00:00+00:00"
    )

    transport = _make_mock_transport([])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    outcomes = await AlertRepository(repo).list_outcomes("a1")
    auto_resolved = [o for o in outcomes if o["outcome"] == AlertOutcome.AUTO_RESOLVED.value]
    assert auto_resolved == []
    matched = [
        e for e in vm.recorded if e.name == "homelab_karma_reconciler_resolutions_matched_total"
    ]
    assert matched and matched[-1].value == 0.0


async def test_lag_gauge_reports_seconds_since_previous_reconciliation(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When last_reconciliation_at is set to a past time, lag gauge > 0."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    past = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
    await AppSettingsRepository(repo).set("analyzer.last_reconciliation_at", past)

    transport = _make_mock_transport([])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    assert result.ok is True
    lag = [
        e
        for e in vm.recorded
        if e.name == "homelab_karma_reconciler_last_reconciliation_lag_seconds"
    ]
    assert lag
    # Allow slack for test scheduling; the gauge should be around 3600s.
    assert lag[-1].value >= 3599.0  # noqa: PLR2004


async def test_silence_with_non_dict_status_is_silently_ignored(
    repo: SqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed silence with status=list crashes-safe — treated as unknown state."""
    monkeypatch.setenv("HOMELAB_MONITOR_ALERTMANAGER_URL", "http://am-test:9093")

    silence: dict[str, Any] = {
        "id": "s1",
        "status": ["not", "a", "dict"],
        "startsAt": "2026-06-01T12:00:00Z",
        "endsAt": "2027-06-01T12:00:00Z",
        "matchers": [_eq_matcher("alertname", "DiskFull")],
    }
    transport = _make_mock_transport([silence])
    vm = MemoryRetainingMetricsWriter()

    async with httpx.AsyncClient(transport=transport) as http:
        result = await KarmaOutcomeReconciler().run(_ctx(repo, http, vm=vm))

    # Should not crash — state defaults to None → silence ignored.
    assert result.ok is True
