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
