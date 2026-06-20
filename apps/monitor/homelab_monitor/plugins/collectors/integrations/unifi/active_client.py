"""unifi_active_client collector -- drives the identity-upsert into the registry.

Polls classic ``stat/sta`` (active clients) and ``stat/alluser`` (all known
clients) once per 60s tick, then calls the STAGE-007-004 helper
``upsert_identity`` INSIDE a caller-owned write transaction to populate the
persistent ``unifi_clients`` registry + IP<->MAC observations. Emits:

- API latency ``homelab_unifi_api_took_seconds{endpoint}`` per successful call.
- UpsertResult self-metrics (clients_upserted / observations_appended /
  hosts_reconciled / skipped).
- A new-client signal: a ``homelab_unifi_new_client_total`` COUNTER incremented
  by the count of macs seen this tick but absent from the prior registry, plus a
  per-mac ``homelab_unifi_new_client{mac,hostname,network}`` info gauge.
- ``homelab_unifi_alluser_degraded`` (1.0 when the stat/alluser call failed).
- Roster rollups computed from the LIVE stat/sta parse (the registry does not
  store essid/radio/is_wired): active/known/offline totals + per-ssid / per-network
  / per-ap / per-band / per-link online counts.

FAILURE SEMANTICS (D2 asymmetric degrade):
- ``ctx.unifi is None`` -> ok=False, errors=["unifi client not configured"].
- ``stat_sta()`` returns UnifiError -> HARD FAIL (ok=False, errors=[msg], NO
  upsert; the online roster is irreplaceable).
- ``stat_alluser()`` returns UnifiError -> PROCEED: upsert with stat_alluser=[],
  emit alluser_degraded=1.0, append the error to result.errors, return ok=True.
- A sta 200-with-malformed-body (payload not a dict, or data not a list) is NOT an
  endpoint failure: proceed with empty sta records (no-op upsert, zero rollups),
  ok=True.

NEW-CLIENT DETECTION (A1): ``prior_macs`` is snapshotted via
``repo.list_clients()`` IMMEDIATELY BEFORE the write transaction. ``list_clients``
opens its own connection (it cannot share the write conn), so the read is one tick
ahead of the write -- a client appearing in that window is flagged new one tick
late, which is acceptable. The host sentinel mac (``host:<ip>``) is excluded from
both sets.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.config import load_unifi_config
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.kernel.unifi.identity import UpsertResult, upsert_identity
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import as_bool

# radio -> human band label for client_count_by_band{band}.
_BAND_BY_RADIO: dict[str, str] = {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}


def _is_sentinel(mac: str) -> bool:
    """Return True for the seeded host sentinel mac (``host:<ip>``)."""
    return mac.startswith("host:")


def _parse_records(payload: object) -> list[dict[str, object]]:
    """Narrow a classic ``{"data":[...]}`` payload to a list of record dicts.

    Returns [] when the payload is not a dict, ``data`` is not a list, or there are
    no dict entries. Non-dict entries are skipped.
    """
    if not isinstance(payload, dict):
        return []
    payload_dict = cast("dict[str, object]", payload)
    data_obj = payload_dict.get("data")
    if not isinstance(data_obj, list):
        return []
    data = cast("list[object]", data_obj)
    return [cast("dict[str, object]", r) for r in data if isinstance(r, dict)]


def _str_field(rec: dict[str, object], key: str) -> str | None:
    """Return rec[key] only if it is a str, else None."""
    val = rec.get(key)
    return val if isinstance(val, str) else None


def _extract_macs(records: list[dict[str, object]]) -> set[str]:
    """Return the set of real (non-sentinel) macs from a list of records."""
    macs: set[str] = set()
    for rec in records:
        mac = _str_field(rec, "mac")
        if mac is None or _is_sentinel(mac):
            continue
        macs.add(mac)
    return macs


def _build_record_by_mac(
    sta_records: list[dict[str, object]],
    alluser_records: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Map mac -> its record, preferring the stat/sta record over stat/alluser."""
    by_mac: dict[str, dict[str, object]] = {}
    for rec in alluser_records:
        mac = _str_field(rec, "mac")
        if mac is not None and not _is_sentinel(mac):
            by_mac[mac] = rec
    for rec in sta_records:
        mac = _str_field(rec, "mac")
        if mac is not None and not _is_sentinel(mac):
            by_mac[mac] = rec
    return by_mac


def _emit_latency(ctx: CollectorContext, endpoint: str, took: float, emitted: list[int]) -> None:
    """Emit the per-endpoint API latency gauge and bump the emitted counter."""
    ctx.vm.write_gauge("homelab_unifi_api_took_seconds", took, {"endpoint": endpoint})
    emitted[0] += 1


def _emit_gauge_by_key(
    ctx: CollectorContext,
    metric_name: str,
    label_key: str,
    by_key: dict[str, int],
    emitted: list[int],
) -> None:
    """Emit gauges for each key in the counter dict."""
    for key, count in by_key.items():
        ctx.vm.write_gauge(metric_name, float(count), {label_key: key})
        emitted[0] += 1


def _emit_self_metrics(ctx: CollectorContext, result: UpsertResult, emitted: list[int]) -> None:
    """Emit the four UpsertResult self-metric gauges."""
    ctx.vm.write_gauge(
        "homelab_unifi_identity_clients_upserted", float(result.clients_upserted), {}
    )
    emitted[0] += 1
    ctx.vm.write_gauge(
        "homelab_unifi_identity_observations_appended",
        float(result.observations_appended),
        {},
    )
    emitted[0] += 1
    ctx.vm.write_gauge(
        "homelab_unifi_identity_hosts_reconciled", float(result.hosts_reconciled), {}
    )
    emitted[0] += 1
    ctx.vm.write_gauge("homelab_unifi_identity_skipped", float(result.skipped), {})
    emitted[0] += 1


def _emit_new_clients(
    ctx: CollectorContext,
    new_macs: set[str],
    record_by_mac: dict[str, dict[str, object]],
    emitted: list[int],
) -> None:
    """Emit the new-client COUNTER (by count) + a per-mac info gauge for each new mac."""
    ctx.vm.write_counter("homelab_unifi_new_client_total", float(len(new_macs)), {})
    emitted[0] += 1
    for mac in new_macs:
        rec = record_by_mac.get(mac, {})
        hostname = _str_field(rec, "hostname") or ""
        network = _str_field(rec, "network") or ""
        ctx.vm.write_gauge(
            "homelab_unifi_new_client",
            1.0,
            {"mac": mac, "hostname": hostname, "network": network},
        )
        emitted[0] += 1


def _emit_rollups(
    ctx: CollectorContext,
    sta_records: list[dict[str, object]],
    current_macs: set[str],
    emitted: list[int],
) -> None:
    """Emit roster rollups from the live stat/sta parse (B2)."""
    valid_sta: list[dict[str, object]] = [
        rec
        for rec in sta_records
        if (_mac := _str_field(rec, "mac")) is not None and not _is_sentinel(_mac)
    ]
    sta_macs = _extract_macs(valid_sta)
    active = len(valid_sta)
    known = len(current_macs)
    offline = known - len(sta_macs)

    ctx.vm.write_gauge("homelab_unifi_active_client_count", float(active), {})
    emitted[0] += 1
    ctx.vm.write_gauge("homelab_unifi_known_client_count", float(known), {})
    emitted[0] += 1
    ctx.vm.write_gauge("homelab_unifi_offline_client_count", float(offline), {})
    emitted[0] += 1

    by_ssid: Counter[str] = Counter()
    by_network: Counter[str] = Counter()
    by_ap: Counter[str] = Counter()
    by_band: Counter[str] = Counter()
    by_link: Counter[str] = Counter()
    by_reservation: Counter[str] = Counter()

    for rec in valid_sta:
        is_wired = as_bool(rec.get("is_wired"))
        by_link["wired" if is_wired else "wireless"] += 1

        network = _str_field(rec, "network")
        if network is not None:
            by_network[network] += 1
            # DHCP reservations: count clients with a fixed IP, grouped by network.
            if as_bool(rec.get("use_fixedip")):
                by_reservation[network] += 1

        # Wireless-only dimensions: essid / ap_mac / radio (band). Wired records
        # lack these -- skip them FOR THOSE DIMENSIONS (not an error).
        if not is_wired:
            essid = _str_field(rec, "essid")
            if essid is not None:
                by_ssid[essid] += 1
            ap_mac = _str_field(rec, "ap_mac")
            if ap_mac is not None:
                by_ap[ap_mac] += 1
            radio = _str_field(rec, "radio")
            if radio is not None:
                band = _BAND_BY_RADIO.get(radio, radio)
                by_band[band] += 1

    _emit_gauge_by_key(ctx, "homelab_unifi_ssid_client_count", "ssid", by_ssid, emitted)
    _emit_gauge_by_key(ctx, "homelab_unifi_client_count_by_network", "network", by_network, emitted)
    _emit_gauge_by_key(ctx, "homelab_unifi_client_count_by_ap", "ap_mac", by_ap, emitted)
    _emit_gauge_by_key(ctx, "homelab_unifi_client_count_by_band", "band", by_band, emitted)
    _emit_gauge_by_key(ctx, "homelab_unifi_client_count_by_link", "link", by_link, emitted)

    # Emit DHCP reservations: one series per network in the by_network universe.
    for network in by_network:
        ctx.vm.write_gauge(
            "homelab_unifi_dhcp_reservation_count",
            float(by_reservation.get(network, 0)),
            {"network": network},
        )
        emitted[0] += 1


class UnifiActiveClientCollector(BaseCollector):
    """Drive stat/sta + stat/alluser into the unifi_clients registry + emit rollups.

    Reads both classic endpoints once per 60s tick, snapshots the prior registry
    macs for new-client detection, then runs ``upsert_identity`` inside a
    caller-owned transaction. Emits the UpsertResult self-metrics, the new-client
    signal, the alluser-degraded gauge, the API latencies, and the roster rollups.
    """

    name: ClassVar[str] = "unifi_active_client"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll stat/sta + stat/alluser, upsert the registry, and emit metrics."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        emitted = [0]
        errors: list[str] = []

        # stat/sta is mandatory (D2): an endpoint failure is a hard fail.
        sta_result = await ctx.unifi.stat_sta()
        if isinstance(sta_result, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=emitted[0],
                errors=[sta_result.message],
                duration_seconds=time.monotonic() - start,
            )
        _emit_latency(ctx, sta_result.endpoint, sta_result.took_seconds, emitted)
        sta_records = _parse_records(sta_result.payload)

        # stat/alluser is best-effort (D2): an endpoint failure degrades, not fails.
        alluser_result = await ctx.unifi.stat_alluser()
        if isinstance(alluser_result, UnifiError):
            alluser_degraded = True
            alluser_records: list[dict[str, object]] = []
            errors.append(alluser_result.message)
        else:
            alluser_degraded = False
            _emit_latency(ctx, alluser_result.endpoint, alluser_result.took_seconds, emitted)
            alluser_records = _parse_records(alluser_result.payload)

        config = load_unifi_config()
        host_ip = config.host_lan_ip
        now_dt = datetime.now(tz=UTC)
        now_iso = now_dt.isoformat()
        cutoff_iso = (now_dt - timedelta(days=config.observation_retention_days)).isoformat()

        # A1: snapshot prior macs BEFORE the write txn (list_clients opens its own
        # connection -- it cannot share the write conn). One-tick-late is acceptable.
        repo = UnifiClientRepo(ctx.db)
        prior_rows = await repo.list_clients()
        prior_macs = {row.mac for row in prior_rows if not _is_sentinel(row.mac)}

        current_macs = _extract_macs(sta_records) | _extract_macs(alluser_records)
        new_macs = current_macs - prior_macs
        record_by_mac = _build_record_by_mac(sta_records, alluser_records)

        async with ctx.db.transaction() as conn:
            result = await upsert_identity(
                conn,
                stat_sta=sta_records,
                stat_alluser=alluser_records,
                host_lan_ip=host_ip,
                observation_cutoff=cutoff_iso,
                now=now_iso,
            )

        _emit_self_metrics(ctx, result, emitted)
        _emit_new_clients(ctx, new_macs, record_by_mac, emitted)
        ctx.vm.write_gauge("homelab_unifi_alluser_degraded", 1.0 if alluser_degraded else 0.0, {})
        emitted[0] += 1
        _emit_rollups(ctx, sta_records, current_macs, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=errors,
            duration_seconds=time.monotonic() - start,
        )
