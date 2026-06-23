# Regression Checklist - EPIC-006: Pi-hole

(Items added per stage during Refinement.)

## STAGE-006-001 — Pi-hole v6 client

- [ ] **Live auth + version read:** From inside the prod `homelab-monitor` container, the real `PiholeRestClient` (base_url `http://192.168.2.148:8080`, secret `pihole_api_password_ro`) authenticates (`POST /api/auth` → SID) and `info_version()` returns a `PiholeResponse` with real version data (Core/Web/FTL) + a `took_seconds` float. (NOT `localhost` — the bridge-network container cannot reach the host's loopback.)
- [ ] **Session reuse:** A second client call (e.g. `info_ftl()`) reuses the SID without re-login (only ONE `POST /api/auth` across both calls).
- [ ] **Logout frees the slot:** `aclose()` issues `DELETE /api/auth` and never raises even if Pi-hole is unreachable at shutdown.
- [ ] **Two secrets stored:** Both `pihole_api_password_ro` and `pihole_api_password_rw` exist in the prod secret store (same value; Pi-hole v6 single app-password tier; RW first exercised in STAGE-006-018).
- [ ] **Vector label:** VictoriaLogs `service:"pihole-unbound"` still returns live FTL log hits (scope for STAGE-006-017 + 006-024).
- [ ] **base_url default is host LAN IP:** `load_pihole_config().base_url` defaults to `http://192.168.2.148:8080` (overridable via `HOMELAB_MONITOR_PIHOLE_URL`), NOT `localhost`.
- [ ] **App password never logged:** No Pi-hole error message or log line ever contains the app-password value.

## STAGE-006-002 — Pi-hole integration bundle skeleton

- [ ] **Bundle registers cleanly:** at monitor startup, NO `pihole_integration.collector_register_failed` warning appears in `docker logs homelab-monitor` (the per-collector try/except did not fire).

## STAGE-006-005 — Core query-stats collector

- [ ] **Collector present + healthy:** `pihole_stats_summary` appears in `GET /api/collectors` (authenticated) healthy with `interval_seconds:30`; the old `pihole_placeholder` is GONE.
- [ ] **Core metrics in VM:** `homelab_pihole_queries_total`, `_blocked_total`, `_forwarded_total`, `_cached_total`, `_percent_blocked`, `_query_frequency`, `_unique_domains`, `_active_clients`, `_total_clients` all present in VictoriaMetrics (`docker exec homelab-vm wget -qO- 'http://127.0.0.1:8428/api/v1/query?query=homelab_pihole_queries_total'`).
- [ ] **Enum families:** `homelab_pihole_query_by_type{type}` (~16), `homelab_pihole_query_by_status{status}` (~19-20), `homelab_pihole_query_by_reply{reply}` (~14-15) emit per-label series faithfully (zero-count labels may be absent that tick).
- [ ] **API latency:** `homelab_pihole_api_took_seconds{endpoint="stats/summary"}` present.
- [ ] **Metric names match the card (NO `queries_` infix):** the scalars are `homelab_pihole_blocked_total` / `_percent_blocked` / `_forwarded_total` / `_cached_total` / `_query_frequency` / `_unique_domains` — NOT `homelab_pihole_queries_blocked_total` etc. (a Refinement correction; downstream alert rules 016/017 + Grafana 026 depend on these exact names).
- [ ] **24h-rolling semantics:** the summary scalars are 24h-rolling window-gauges, NOT lifetime counters — alert rules must NOT `rate()` them.
- [ ] **`homelab_pihole_unique_clients` NOT emitted:** retracted (no source in Pi-hole v6); `_active_clients`/`_total_clients` cover distinct-client info.
- [ ] **Live values sane vs Pi-hole web UI:** VM values for queries_total/blocked/percent_blocked are in-ballpark with the live `/api/stats/summary` (small deltas from ongoing traffic are expected).

## STAGE-006-003 — Unbound-control access layer

- [ ] **Live exec+parse:** From inside the prod `homelab-monitor` container, `fetch_unbound_stats(exec_backend=DockerSocketClient("/var/run/docker.sock"), container="pihole-unbound")` returns `UnboundStats` (not `UnboundError`) with `extended_enabled=True` and real values (`raw["total.num.queries"]` > 0).
- [ ] **Demux integrity:** `UnboundStats.raw_line_count` equals `docker exec pihole-unbound unbound-control stats_noreset | wc -l` — the `_demux_stream` parses every line of the real multiplexed Docker exec stream with no loss.
- [ ] **Extended detection:** with `extended-statistics: yes` live, `extended_enabled=True` and `histogram.*` + `num.query.type.*` keys are present in `raw`. (If extended-stats were disabled, `extended_enabled` would be `False` and those keys absent — NOT an error.)
- [ ] **Graceful degrade:** `fetch_unbound_stats(..., container="nonexistent-xyz")` → `UnboundError(reason="container_unreachable")`; a docker-socket/perm failure → `socket_error`; unbound-control nonzero exit → `control_error`. Never raises into the caller.
- [ ] **Consumer note:** `fetch_unbound_stats` is consumed by STAGE-006-013 (Unbound stats collector) which emits `homelab_pihole_unbound_extended_stats_enabled` from `extended_enabled`.

## STAGE-006-004 — Per-client cardinality + loopback-attribution helper

- [ ] **Loopback structural exemption:** `kernel/pihole/clients.py` loopback clients (`127.0.0.1`, `::1`, `::`, name `pi.hole`/`localhost`) are NEVER dropped by the cardinality cap (partitioned out before the capper). Verified at cap=50 with 200 LAN clients and at cap=0.
- [ ] **Empty host_lan_ip override:** With `pihole_host_lan_ip` empty (public-release default), every loopback client classifies as `client_kind="unattributed"` with `host_lan_ip=None` — overriding even resolver-name (`pi.hole`/`localhost`) matches.
- [ ] **Non-empty host_lan_ip stamping:** With `pihole_host_lan_ip` set (e.g. `192.168.2.148`), loopback-by-name `pi.hole`/`localhost` → `resolver_self`; other loopback (bare IP or non-resolver name) → `local`; both stamped with `host_lan_ip`.
- [ ] **Deterministic LAN eviction:** `classify_clients` LAN eviction is deterministic — same client set yields identical survivors regardless of input order (stable-sort-first-N via the reused `CardinalityCapper`).
- [ ] **Case and MAC preservation:** `ClassifiedClient.client_name` preserves the ORIGINAL-case name and `client_mac` passes through verbatim (classification lowercases internally for comparison only).
- [ ] **Domain cap determinism:** `cap_domains` caps top-domain series deterministically (no loopback exemption — domains have no loopback concept).
- [ ] **Config envvar wiring:** config `HOMELAB_MONITOR_PIHOLE_HOST_LAN_IP` env → `PiholeConfig.host_lan_ip` (empty default when unset).

## STAGE-006-006 — Upstreams collector

- [ ] **Collector present:** `pihole_upstreams` registered + healthy (`GET /api/collectors`, `interval_seconds:30`).
- [ ] **Upstream metric in VM:** `homelab_pihole_upstream_queries{upstream}` present with one series per forward destination (`docker exec homelab-vm wget -qO- 'http://127.0.0.1:8428/api/v1/query?query=homelab_pihole_upstream_queries'`). The REAL unbound upstream `upstream="127.0.0.1#5335"` MUST be present.
- [ ] **Pseudo-upstream labels:** `upstream="cache"` and `upstream="blocklist"` present as BARE names (NOT `cache#-1`); real upstreams use `ip#port` format. (Pseudo = `port == -1` in the API.)
- [ ] **016 alert contract:** the `PiholeUpstreamAllDown` / `PiholeUpstreamDown` rules (STAGE-006-016) MUST exclude `upstream="cache"` and `upstream="blocklist"` so the alert fires only when the REAL resolver share → 0.
- [ ] **API latency:** `homelab_pihole_api_took_seconds{endpoint="stats/upstreams"}` present.
- [ ] **24h-rolling semantics:** `count` is a 24h-rolling window-gauge, NOT a counter — alert rules must NOT `rate()` it.
- [ ] **Omitted by design:** NO `kind`/`is_pseudo` label, NO `homelab_pihole_upstream_response_seconds`, NO duplicate top-level totals (005 owns `queries_total`/`forwarded_total`).
- [ ] **Live values sane vs Pi-hole:** VM upstream counts in-ballpark with live `/api/stats/upstreams` (small deltas from ongoing traffic expected).

## STAGE-006-007 — Gravity + per-adlist collector

- [ ] **Collector present:** `pihole_gravity` registered + healthy (`GET /api/collectors`, `interval_seconds:30`).
- [ ] **Gravity domains:** `homelab_pihole_gravity_domains` present in VM, the DEDUP'd count from `ftl.database.gravity` (~5.86M), NOT a sum of per-list `number` (`docker exec homelab-vm wget -qO- 'http://127.0.0.1:8428/api/v1/query?query=homelab_pihole_gravity_domains'`).
- [ ] **Gravity age:** `homelab_pihole_gravity_last_update_age_seconds` present, positive/sane, DERIVED from `max(date_updated)` across adlists (the `/api/info/ftl` endpoint has NO timestamp; age comes from `/api/lists`). Skipped (not 0) if no adlist has a valid `date_updated`.
- [ ] **Per-adlist metrics:** `homelab_pihole_adlist_domains{list,address}`, `_adlist_enabled{list,address}` (1/0), `_adlist_status{list,address,status}` — one series per adlist; series keyed by `id`, `address` carried as a label, NO `comment` label.
- [ ] **Failing adlists by name:** the 2 live failing adlists (id=4, id=5, the xRuffKez NRD GitHub URLs) surface as `homelab_pihole_adlist_status{status="parse_failed"}`; the 3 healthy lists carry `status="ok"`. (v6 status int map: 0=not_run, 1=ok, 2=download_failed, 3=parse_failed; unknown→`unknown_<n>`.)
- [ ] **016 alert contract:** `PiholeAdlistFailing` matches `homelab_pihole_adlist_status{status!="ok"} == 1`; `PiholeGravityStale` thresholds on `_gravity_last_update_age_seconds`. No TZ-guard needed — the source timestamps are epoch seconds (unambiguous UTC); only a clock-skew `max(0.0)` clamp applies.
- [ ] **Two-endpoint resilience:** the collector polls BOTH `/api/info/ftl` and `/api/lists`; emits two `homelab_pihole_api_took_seconds{endpoint}` (info/ftl + lists); `ok=True` if EITHER endpoint succeeds, `ok=False` only if BOTH error.
- [ ] **Live values sane vs Pi-hole:** VM `gravity_domains` exactly matches live `ftl.database.gravity`; failing-adlist ids + per-list `number` match the live `/api/lists`.

## STAGE-006-008 — Blocking-state collector

- [ ] **Pi-hole blocking-state collector (`pihole_blocking`) emits `homelab_pihole_blocking_enabled`** (1.0 when `blocking=="enabled"`, 0.0 otherwise — fail-closed for disabled/failed/unknown/non-string/missing). Verify in VM: `homelab_pihole_blocking_enabled` is present with value matching the live Pi-hole blocking toggle.
- [ ] **`homelab_pihole_blocking_timer_seconds` omitted when no timer active:** the series is emitted ONLY when a temporary-disable timer is active (non-null `timer`); it is OMITTED (not zeroed) when no timer is active. Verify: with blocking enabled / no timer, the series is ABSENT in VM (empty query result), not `0`.
- [ ] **API latency metric present:** `homelab_pihole_api_took_seconds{endpoint="dns/blocking"}` is emitted each run with a small positive value.
- [ ] **Self-metric correct label:** Self-metric `homelab_collector_run_success_total{name="pihole_blocking"}` increments on each successful run (label key is `name`, not `collector`).
- [ ] **Fail-closed semantics on enum edge cases:** a `blocking` value other than `"enabled"` (incl. `disabled`, `failed`, `unknown`, unrecognized, or non-string/missing) yields `homelab_pihole_blocking_enabled == 0`.

## STAGE-006-009 — FTL health + DB collector

- [ ] **FTL-health collector (`pihole_ftl_health`) emits `homelab_pihole_ftl_uptime_seconds`, `_ftl_cpu_percent`, `_ftl_memory_percent`, `_privacy_level` from `/api/info/ftl` — verify present in VM with sane values. CRITICAL: these read from the NESTED `payload["ftl"]` object (not top-level) — a regression to top-level reads would silently drop all of them.
- [ ] **`homelab_pihole_dnsmasq_cache_insertions` / `_evictions` emitted from `ftl.dnsmasq.dns_cache_inserted` / `dns_cache_live_freed` — verify present.
- [ ] **`homelab_pihole_db_size_bytes` (from `size`) and `homelab_pihole_db_queries_total` (from `queries_disk`, the on-disk total ~11.6M — NOT `queries` ~97k) — verify present and that db_queries_total is the large on-disk number.
- [ ] **Per-endpoint resilience: `ok = ftl_ok or db_ok` — run succeeds if at least one of `/api/info/ftl` / `/api/info/database` succeeds.
- [ ] **Does NOT double-emit `homelab_pihole_gravity_domains` (STAGE-007 owns it) and does NOT emit host cpu/mem (scope-out).
- [ ] **KNOWN FLAKY (pre-existing, unrelated): `tests/test_scheduler.py::test_process_run_kind` is order-dependent — can fail in a full `make verify` run, passes in isolation/on re-run. If it fails, re-run before investigating; it is NOT caused by Pi-hole collector work.
- [ ] **CLIENT HARDENING GAP (tracked to STAGE-006-018): `PiholeRestClient._get()` does not detect a 200-response carrying an `{"error": {...}}` body; all pihole collectors would silently emit nothing if Pi-hole returns 200-with-error-envelope. Verify STAGE-006-018 adds 200-error-body detection to the client.

## STAGE-006-010 — FTL diagnostic-messages collector

- [ ] FTL diagnostic-messages collector (`pihole_ftl_messages`) emits `homelab_pihole_messages_count` (= total list length, always emitted incl. 0 when no messages) — verify present in VM, value matches the live Pi-hole message count.
- [ ] `homelab_pihole_messages{type}` emits a per-type COUNT, grouped by the message `type` field (duplicate types collapse: 2 LIST messages → `{type="LIST"}=2`, NOT two series). Verify grouping in VM matches the live by-type breakdown. Present types only (no zero-fill).
- [ ] Non-string/missing `type` falls back to `{type="unknown"}`; non-dict message entries are skipped (counted in messages_count total but not in any per-type series, so sum(per-type) may be < count when malformed entries exist).
- [ ] `homelab_pihole_api_took_seconds{endpoint="info/messages"}` emitted each run.
- [ ] Self-metric `homelab_collector_run_success_total{name="pihole_ftl_messages"}` increments on each successful run.
- [ ] Malformed-payload resilience: payload not a dict, or "messages" key missing/not-a-list → run reports ok=False with an error (api_took still counted); does NOT falsely emit messages_count=0.
- [ ] Metric name is PLURAL `homelab_pihole_messages{type}` (corrected from the card's singular `homelab_pihole_message` during Design — per-type count semantics).
