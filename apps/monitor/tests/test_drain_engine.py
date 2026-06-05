"""Tests for DrainEngine end-to-end (STAGE-004-025).

Uses real SqlitePersistence over a migrated temp DB (`repo` fixture). Verifies:
clustering, is_new/first_seen semantics, model bucketing, snapshot() persistence,
restart replay via a fresh engine over the same persistence, and templates().
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.logs.drain_engine import DrainEngine
from homelab_monitor.kernel.logs.drain_persistence import SqlitePersistence
from homelab_monitor.kernel.logs.models import LogLine


def _line(
    msg: str,
    *,
    service: str | None = "pihole",
    ts: str = "2026-06-05T12:00:00Z",
    fields: dict[str, Any] | None = None,
) -> LogLine:
    return LogLine(
        timestamp=ts,
        message=msg,
        stream="stdout",
        severity="info",
        host="h",
        service=service,
        fields=fields or {},
    )


async def test_similar_lines_cluster_to_same_hash(repo: SqliteRepository) -> None:
    # drain3 generalises the cluster template on the *second* distinct line
    # (change_type="cluster_template_changed"). The first line uses the literal
    # as the template; the second generalises it. Only from the third line onward
    # do repeated same-cluster lines share the same (generalized) hash and trigger
    # is_new=False. Feed two priming lines, then assert on the third.
    engine = DrainEngine(SqlitePersistence(repo))
    await engine.add_line(_line("query A1B2 from 10.0.0.5 took 12 ms"))
    e_prime = await engine.add_line(_line("query C3D4 from 10.0.0.9 took 40 ms"))
    e3 = await engine.add_line(_line("query E5F6 from 10.0.0.3 took 7 ms"))
    assert e_prime.template_hash == e3.template_hash
    assert e_prime.is_new is True  # generalized template is a new signature
    assert e3.is_new is False


async def test_distinct_kinds_distinct_templates(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    dns = await engine.add_line(_line("resolved example.com to 1.2.3.4"))
    err = await engine.add_line(_line("FATAL database connection refused on port 5432"))
    assert dns.template_hash != err.template_hash


async def test_distinct_services_distinct_models(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    a = await engine.add_line(_line("boot complete", service="homeassistant"))
    b = await engine.add_line(_line("boot complete", service="pihole"))
    assert a.model_key == "homeassistant"
    assert b.model_key == "pihole"
    assert set(engine._models.keys()) == {"homeassistant", "pihole"}  # pyright: ignore[reportPrivateUsage]


async def test_is_new_true_then_false_on_repeat(repo: SqliteRepository) -> None:
    # drain3 fires cluster_template_changed on the second distinct line, producing a
    # new generalized template hash (is_new=True again). Only the third line hits the
    # already-generalized cluster with is_new=False. The first_seen_ts of the
    # generalized template is anchored at the second line's timestamp.
    engine = DrainEngine(SqlitePersistence(repo))
    await engine.add_line(_line("user admin logged in", ts="2026-06-05T12:00:00Z"))
    second = await engine.add_line(_line("user bob logged in", ts="2026-06-05T12:05:00Z"))
    third = await engine.add_line(_line("user carol logged in", ts="2026-06-05T12:10:00Z"))
    assert second.is_new is True  # generalized template = new signature
    assert third.is_new is False
    # first_seen_ts of the generalized template is anchored at second's timestamp.
    assert third.first_seen_ts == second.first_seen_ts
    assert third.last_seen_ts != second.last_seen_ts


async def test_first_seen_ts_parsed_from_iso(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    ev = await engine.add_line(_line("hello world", ts="2026-06-05T12:00:00+00:00"))
    # 2026-06-05T12:00:00Z == 1780660800000 ms (compute via datetime in the test).
    from datetime import UTC, datetime  # noqa: PLC0415

    expected = int(datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert ev.first_seen_ts == expected
    assert ev.last_seen_ts == expected


async def test_naive_timestamp_treated_as_utc(repo: SqliteRepository) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    engine = DrainEngine(SqlitePersistence(repo))
    ev = await engine.add_line(_line("naive ts line", ts="2026-06-05T12:00:00"))
    expected = int(datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert ev.first_seen_ts == expected


async def test_malformed_timestamp_does_not_crash(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    ev = await engine.add_line(_line("ok", ts="not-a-timestamp"))
    assert ev.is_new is True
    assert ev.first_seen_ts > 0  # fell back to _now_ms()


async def test_hmrun_cron_line_buckets_to_cron_model(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    ev = await engine.add_line(
        _line("backup finished ok", service="hmrun", fields={"command": "/bin/backup.sh"})
    )
    assert ev.model_key.startswith("cron:")


async def test_snapshot_then_fresh_engine_replays_state(repo: SqliteRepository) -> None:
    # Prime two lines so drain3 generalises the template before snapshotting.
    # That way first_seen stores the generalized hash, and the fresh engine can
    # match a third similar line as not-new.
    persistence = SqlitePersistence(repo)
    engine = DrainEngine(persistence)
    await engine.add_line(_line("query Z from 10.0.0.1 took 5 ms", ts="2026-06-05T12:00:00Z"))
    await engine.add_line(_line("query W from 10.0.0.2 took 8 ms", ts="2026-06-05T12:01:00Z"))
    await engine.snapshot()

    # Fresh engine over the SAME persistence: the generalized template hash is in the
    # restored first_seen_map, so a new similar line is NOT new.
    engine2 = DrainEngine(persistence)
    ev = await engine2.add_line(_line("query Y from 10.0.0.3 took 9 ms", ts="2026-06-05T13:00:00Z"))
    assert ev.is_new is False
    assert ev.first_seen_ts < ev.last_seen_ts


async def test_templates_returns_loaded_model_templates(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    await engine.add_line(_line("alpha 1 beta", service="svcA"))
    await engine.add_line(_line("alpha 2 beta", service="svcA"))
    await engine.add_line(_line("gamma delta epsilon zeta", service="svcA"))
    templates = engine.templates("svcA")
    assert len(templates) >= 1
    for t in templates:
        assert t.model_key == "svcA"
        assert t.template_hash == hashlib.sha256(t.template_str.encode("utf-8")).hexdigest()


async def test_templates_unloaded_model_returns_empty(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    assert engine.templates("never-loaded") == []


async def test_custom_model_key_fn_is_honoured(repo: SqliteRepository) -> None:
    def fixed_key(_line: LogLine) -> str:
        return "fixed-bucket"

    engine = DrainEngine(SqlitePersistence(repo), model_key_fn=fixed_key)
    ev = await engine.add_line(_line("anything", service="pihole"))
    assert ev.model_key == "fixed-bucket"


async def test_get_model_caches_instance(repo: SqliteRepository) -> None:
    engine = DrainEngine(SqlitePersistence(repo))
    m1 = await engine.get_model("svcCache")
    m2 = await engine.get_model("svcCache")
    assert m1 is m2  # second call returns the cached _Model (covers the cache-hit branch)


async def test_corrupt_snapshot_blob_degrades_to_fresh_miner(
    repo: SqliteRepository,
) -> None:
    """A corrupt snapshot BLOB (invalid drain3 state) does not brick the model.

    The engine catches any exception during miner construction and degrades to a
    fresh miner, preserving first_seen_map history. A subsequent add_line succeeds
    and the model re-clusters from scratch.
    """
    persistence = SqlitePersistence(repo)
    # Persist a row with a deliberately corrupt snapshot BLOB and a valid first_seen_map.
    await persistence.persist(
        model_key="pihole",
        snapshot=b"not-a-valid-drain3-blob",  # corrupt
        line_count=0,
        template_count=0,
        last_processed_ts=None,
        first_seen_map_json=json.dumps({"abc": 123}),
        updated_at=1,
    )
    # Construct a fresh engine and add a line for the corrupt model_key.
    engine = DrainEngine(persistence)
    ev = await engine.add_line(_line("test message", service="pihole"))
    # Should not raise; should degrade to fresh miner and return a valid event.
    assert ev.model_key == "pihole"
    assert ev.is_new is True  # first line to the fresh miner
    # Verify first_seen_map was preserved (the old "abc" hash is still there,
    # even though the miner was reset).
    model = await engine.get_model("pihole")
    assert "abc" in model.first_seen
    assert model.first_seen["abc"] == 123  # noqa: PLR2004


async def test_concurrent_cold_load_same_model_key_serializes_via_lock(
    repo: SqliteRepository,
) -> None:
    """Two concurrent add_line calls for the same uncached model_key do not race.

    The double-checked lock in get_model ensures that both calls load the model once
    and share the same _Model instance, so both lines land in the same miner.
    """
    engine = DrainEngine(SqlitePersistence(repo))
    # Add two lines concurrently for the same cold model_key.
    ev1, ev2 = await asyncio.gather(
        engine.add_line(_line("query A from 10.0.0.1 took 5 ms")),
        engine.add_line(_line("query B from 10.0.0.2 took 8 ms")),
    )
    # Both should be in the same model.
    assert ev1.model_key == ev2.model_key == "pihole"
    # Verify the model exists and has both lines.
    model = await engine.get_model("pihole")
    assert model.line_count == 2  # noqa: PLR2004  # both lines landed in the same miner
    # A third similar line should not be new (same generalized template).
    ev3 = await engine.add_line(_line("query C from 10.0.0.3 took 10 ms"))
    assert ev3.is_new is False
