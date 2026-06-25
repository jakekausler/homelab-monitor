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

## STAGE-006-022 — Pi-hole panel write widgets (blocking / gravity / messages / privacy banner)

- [ ] Integrations → Pi-hole Overview tab renders the three real widgets (Blocking control, Gravity & adlists, FTL messages) in place of the prior placeholders.
- [ ] Blocking control: shows live blocking state (on/off + countdown when a timer is active); "Disable" / "Enable" buttons open a confirm dialog requiring the typed phrase ("disable"/"enable"); the confirm button is disabled until the phrase matches.
- [ ] After a successful Disable/Enable, the widget AND the header status-strip badge flip to the new state IMMEDIATELY and STAY (no flicker back to the old state) — the mutation patches the overview cache from the authoritative POST response and invalidates with `refetchType: 'none'` so the stale VM metric doesn't clobber it; reconciles on the 30s refetch.
- [ ] Disable with a timer preset (30s/5m/1h) sends `timer`; "Indefinite" omits `timer`; the live re-enable countdown shows after a timed disable.
- [ ] Gravity & adlists widget: shows gravity domain count + last-update age + a per-adlist health table; failing adlists (e.g. the 2 NRD `parse_failed` lists) are visually distinct (critical badge + row treatment); responsive (table on desktop, stacked on mobile).
- [ ] "Update gravity now" button: confirm phrase "update"; the button shows a pending spinner during the (~120s) gravity rebuild; on success a result dialog shows the `log_tail`; a toast appears on non-400 error. (Manual: only exercise the real rebuild deliberately.)
- [ ] FTL diagnostic-messages widget: lists messages (type badge + text + optional timestamp/url); "No diagnostic messages" empty state when healthy.
- [ ] Privacy banner: shown above the widgets ONLY when `privacy_level > 0`; hidden when 0 or null.
- [ ] Write security contract (backend, re-verified live): POST /blocking + /gravity/update require auth + CSRF (403 without `X-CSRF-Token`) + the exact confirm phrase (400 on mismatch); actions are audited.
- [ ] Desktop (1280×720) + Mobile (375×667): all widgets + the confirm dialogs lay out cleanly, no overflow.

## STAGE-006-011 — Version/update collector

- [ ] Version collector (`pihole_version`) emits `homelab_pihole_update_available{component}` (1/0) — `1` when local != remote, `0` when equal, emitted ONLY when BOTH versions present (missing either → no series, never a false 0). Verify in VM: components with updates show 1, up-to-date show 0, matching the live Pi-hole `/api/info/version`.
- [ ] `homelab_pihole_version_info{component, version}` info-gauge (value 1.0, LOCAL version as the `version` label) emitted whenever local present. Verify all present components have a series with the correct installed-version label — INCLUDING docker, whose `local`/`remote` are BARE STRINGS (not objects with a `.version` sub-key); a regression to a uniform `local.version` accessor would drop docker.
- [ ] `homelab_pihole_api_took_seconds{endpoint="info/version"}` emitted each run.
- [ ] Self-metric `homelab_collector_run_success_total{name="pihole_version"}` increments on each successful run.
- [ ] STARTUP LATENCY (tracked to STAGE-006-020): the `pihole_version` collector (3600s interval, no startup-run hook) leaves version metrics absent for up to ~1h after a monitor restart. Sibling slow update-checkers run on startup via `lifespan.py` `await_immediate_run` blocks. Evaluate at STAGE-006-020 whether slow pihole collectors should get a startup-run hook (a cross-cutting lifespan change).

## STAGE-006-012 — Per-client collector

- [ ] `pihole_clients` collector is registered and runs without error: `homelab_collector_run_success_total{name="pihole_clients"} > 0` and `homelab_collector_run_error_total{name="pihole_clients"}` is 0/absent.
- [ ] Per-client metrics present in VM: `homelab_pihole_client_queries` and `homelab_pihole_client_blocked` emit ≈ the live Pi-hole top-client count (capped at 50 LAN + loopback exempt). Series count must NOT collapse to a small number (regression guard for the labelnames_mismatch drop bug — see item 5).
- [ ] Top-domain metrics present: `homelab_pihole_top_permitted_domain{domain}` and `homelab_pihole_top_blocked_domain{domain}`, each capped at 50; when Pi-hole returns >50 domains, `homelab_metric_family_dropped_series{family="homelab_pihole_top_permitted_domain"}` (and `_top_blocked_domain`) > 0.
- [ ] All 5 new api_took endpoint labels present on `homelab_pihole_api_took_seconds`: `stats/top_clients`, `stats/top_clients_blocked`, `stats/top_domains`, `stats/top_domains_blocked`, `network/devices`.
- [ ] STABLE LABEL SET (bug-catch regression): the collector must emit a FIXED 5-key label set on `homelab_pihole_client_queries`/`_client_blocked` (`client_ip`, `client_name`, `client_kind`, `host_lan_ip`, `client_mac`) — using `""` for absent host_lan_ip/client_mac, NOT omitting the key. Verify at the EXPOSITION layer (the collector `/metrics` output), since VictoriaMetrics drops empty-string labels on storage (a `""` label == absent label per the Prometheus data model — do NOT treat "host_lan_ip absent in VM storage" as a failure). Check: `docker exec homelab-vmagent wget -qO- http://monitor:9090/metrics | grep '^homelab_pihole_client_queries'` — every line carries all 5 keys. (Original bug: omit-when-None caused prometheus_writer labelnames_mismatch → ~37 of 49 client series silently dropped.)
- [ ] `client_mac` may legitimately be `ip-<addr>` (e.g. `ip-::`) — this is Pi-hole FTL's synthetic hardware-address for MAC-less / loopback clients, passed through verbatim from the API. It is NOT a homelab-monitor bug; do not "fix" it.
- [ ] The 4 drop-gauge families always emitted (even 0): `homelab_metric_family_dropped_series{family}` for `homelab_pihole_client_queries`, `homelab_pihole_client_blocked`, `homelab_pihole_top_blocked_domain`, `homelab_pihole_top_permitted_domain`.

## STAGE-006-013 — Unbound stats collector

- [ ] `unbound_stats` collector is registered and runs without error: `homelab_collector_run_success_total{name="unbound_stats"} > 0` and `homelab_collector_run_error_total{name="unbound_stats"}` is 0/absent.
- [ ] Default-set metrics present in VM (always, even if extended-stats off): `homelab_unbound_queries_total`, `_cache_hits_total`, `_cache_misses_total`, `_cache_hit_ratio` (≈ hits/(hits+misses)), `_prefetch_total`, `_recursion_time_seconds{quantile="avg"}` + `{quantile="median"}`, `_requestlist_current`, `_requestlist_exceeded_total`.
- [ ] Extended-stats flag: `homelab_pihole_unbound_extended_stats_enabled` = 1 when unbound extended-statistics is on (it is on this host), 0 when off. Note the `homelab_pihole_` prefix on THIS metric (deliberate, bundle-scoped) vs `homelab_unbound_` on the rest.
- [ ] Extended-only metrics present when extended on: `homelab_unbound_recursion_time_seconds{quantile="0.5"|"0.95"|"0.99"}` (histogram-derived, ordered p50≤p95≤p99, plausible seconds), `homelab_unbound_query_type{type}` (A/AAAA/DS/DNSKEY/HTTPS/etc.), `homelab_unbound_answer_rcode{rcode}` (NOERROR/NXDOMAIN/SERVFAIL/REFUSED/FORMERR/NOTIMPL/lowercase `nodata`), `homelab_unbound_answer_secure_total`, `homelab_unbound_answer_bogus_total`.
- [ ] api_took self-instrumentation: `homelab_pihole_api_took_seconds{endpoint="unbound/stats_noreset"}` present (collector wall-clock measurement — the access layer provides no timing).
- [ ] Source-key fallback: the collector reads `total.*` preferred, `thread0.*` fallback (verify metrics emit on a single-thread unbound where total==thread0; if a future multi-thread unbound only exposes `thread0.*` without `total.*`, the fallback keeps metrics flowing).
- [ ] Histogram quantile derivation: `_recursion_time_seconds{quantile="0.95"}` etc. are derived by linear interpolation over the unbound `histogram.*` log-scale buckets; on an empty/zero-total histogram the quantiles are SKIPPED (not emitted as 0) while avg/median still emit. `num.rrset.bogus` is intentionally NOT emitted (not in card scope).
- [ ] Docker-exec dependency: the collector execs `unbound-control stats_noreset` inside the `pihole-unbound` container via the shared docker socket (`app.state.docker_socket_client`). When docker is disabled (no socket injected), the collector degrades cleanly (`ok=False, errors=["client_unconfigured"]`, 0 emits) — it does NOT crash.

## STAGE-006-014 — DNS health probe (composite up + direct :53 probe)

- [ ] Collector `pihole_dns_health` is registered: `register_all` loads it into the pihole bundle (it appears in `_PIHOLE_COLLECTORS`); `make verify` bundle-registration test passes.
- [ ] After `make dev-prod` + `POST /api/collectors/pihole_dns_health/retry`, the monitor `/metrics` emits `homelab_pihole_up 1.0` against the live Pi-hole DNS at `192.168.2.148:53` (proves UDP egress from the bridge network still works).
- [ ] `/metrics` emits `homelab_pihole_dns_probe_result{outcome="ok"} 1.0` on a healthy probe (one-hot outcome series; exactly one outcome series per run).
- [ ] `/metrics` emits `homelab_pihole_dns_probe_seconds` with a small positive value (sane LAN latency, e.g. ~0.001–0.5s) when the probe gets a response; the latency metric is OMITTED on a no-response outcome (timeout/socket_error/malformed/id_mismatch).
- [ ] The composite is DNS-decisive and independent of the Pi-hole REST API: `homelab_pihole_up` is driven by the direct `:53` probe alone (the API-reachability signal is NOT folded in) — verify by code-reading `dns_health.py` that `up` derives only from `DnsProbeResult.ok`.
- [ ] The DNS query primitive `kernel/dns/resolver.py::resolve_a(resolver_ip, qname, *, port=53, timeout_seconds)` is parameterized by resolver IP (reusable by STAGE-006-015's split-check) and NEVER raises — maps timeout/OSError/malformed/id-mismatch to a typed `DnsProbeResult`.
- [ ] Resolver host/port come from config: `PiholeConfig.dns_host` (env `HOMELAB_MONITOR_PIHOLE_DNS_HOST`, defaults to deriving from base_url hostname when empty) + `dns_port` (env `HOMELAB_MONITOR_PIHOLE_DNS_PORT`, default 53) — `make verify` config tests cover the explicit-env, empty→derive, and urlparse-hostname branches.
- [ ] `make verify` GREEN with 100% branch coverage including `kernel/dns/resolver.py` (note the provably-unreachable `finally`-block branch carries `# pragma: no branch` with an inline proof; do NOT remove it).

## STAGE-006-015 — DNS split-check (Pi-hole vs direct 1.1.1.1)

- [ ] Collector `pihole_dns_split` is registered (appears in `_PIHOLE_COLLECTORS`); `make verify` bundle-registration test passes.
- [ ] After `make dev-prod` + `POST /api/collectors/pihole_dns_split/retry`, `/metrics` emits `homelab_dns_resolution_up{path="pihole"} 1.0` AND `homelab_dns_resolution_up{path="direct"} 1.0` (both paths resolve against the live Pi-hole `:53` and Cloudflare `1.1.1.1:53`).
- [ ] **WAN `:53` interception guard (CRITICAL):** `dig @1.1.1.1 CH TXT id.server +short` returns a Cloudflare PoP code (e.g. `"atl15"`), NOT a local resolver identifier — proving the "direct" path actually reaches Cloudflare and is NOT silently DNAT-redirected to the local resolver. If this returns empty / a non-Cloudflare identifier, the split-check is compromised (set `HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_HOST`/`_PORT` to a non-intercepted anchor).
- [ ] `/metrics` emits `homelab_dns_resolution_probe_result{path="pihole",outcome="ok"} 1.0` AND `{path="direct",outcome="ok"} 1.0` on a healthy cycle (one-hot, exactly one outcome series per path).
- [ ] `/metrics` emits `homelab_dns_resolution_seconds{path="pihole"}` and `{path="direct"}` with sane latencies (pihole ~sub-ms local; direct ~few-to-tens-of-ms WAN round-trip — a direct latency ≈ the pihole latency would itself hint at interception). Latency is OMITTED on a no-response outcome (timeout/socket_error/malformed/id_mismatch).
- [ ] 015 is a STATELESS dual-emitter: it computes NO divergence — verify by code-reading `dns_split.py` that it only emits per-path series. The `PiholeDnsSplitDivergence` logic (`up{path="pihole"}==0 and up{path="direct"}==1`) lives in STAGE-006-016 PromQL.
- [ ] Both-fail (WAN down) emits `up{path="pihole"}=0` AND `up{path="direct"}=0` with no special handling — STAGE-006-016 must NOT fire `PiholeDnsSplitDivergence` in that case (direct==1 term is false).
- [ ] Direct-resolver config: `PiholeConfig.direct_dns_host` (env `HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_HOST`, default `1.1.1.1`) + `direct_dns_port` (env `HOMELAB_MONITOR_PIHOLE_DIRECT_DNS_PORT`, default 53; non-numeric raises ValueError) — `make verify` config tests cover explicit/empty-default/non-numeric branches. The shared DNS outcome constants live in `kernel/dns/outcomes.py` (imported by BOTH the 014 and 015 collectors — do NOT re-duplicate them).

## STAGE-006-016 — vmalert-metrics alert rules (22 Pi-hole/unbound/dns rules)

- [ ] `deploy/vmalert/metrics/pihole.yaml` has 22 alert rules in 3 groups (`pihole` 16 / `unbound` 4 / `dns` 2); `docker run --rm --entrypoint promtool -v $(pwd)/deploy/vmalert/metrics:/rules prom/prometheus:v2.47.0 check rules /rules/pihole.yaml` → `SUCCESS: 22 rules found`.
- [ ] The promtool test mirror passes: `docker run --rm --entrypoint promtool -v $(pwd)/deploy/vmalert/metrics:/rules prom/prometheus:v2.47.0 test rules /rules/__tests__/pihole.tests.yaml` → `SUCCESS` (fire + no-fire per rule). NOTE: vmalert rule tests are NOT run by `make verify` — run promtool MANUALLY.
- [ ] On the live prod vmalert-metrics (bind-mounts `deploy/vmalert/metrics/`, hot-reloads every 30s via `-configCheckInterval=30s`), all 22 rules load `health=ok` with empty `lastError`: `docker exec homelab-monitor curl -s http://vmalert-metrics:8880/api/v1/rules | jq -r '.data.groups[] | select(.name=="pihole" or .name=="unbound" or .name=="dns") | .rules[] | "\(.name) \(.health) \(.lastError)"'`.
- [ ] NO false-fire at the real baseline: PiholeClientFlooding (threshold `>300000`, top real client ~77k/24h) and UnboundCacheHitLow (threshold `<0.01`, real ratio ~4.3%) are `inactive`. These were TUNED at 3b from the shipped defaults (50000 / 0.5) which false-fired — do NOT revert.
- [ ] The 3 evaluability-trap rules fire correctly (proven via promtool fire-tests): PiholeUpstreamAllDown uses `absent(...{upstream!~"cache|blocklist"} > 0)` (NOT `count()==0`); PiholeDnsSplitDivergence uses `...{path="pihole"}==0 and on() ...{path="direct"}==1` (the `and on()` is load-bearing); PiholeContainerDown uses `absent(...{state="running"}) or ...{state=~"exited|dead|restarting"}==1`.
- [ ] Upstream rules exclude the always-populated pseudo-upstreams: PiholeUpstreamDown / PiholeUpstreamAllDown carry `upstream!~"cache|blocklist"`.
- [ ] unbound extended-stats are ACTIVE on this rig (NOT off as Design first assumed): `homelab_unbound_answer_rcode` has series, `homelab_unbound_cache_hit_ratio` ~4.3%. The 4 unbound rules are LIVE; at baseline UnboundServfailSpike/UnboundDnssecBogusSpike/UnboundRecursionSlow are correctly `inactive` (SERVFAIL~0, bogus=0, p95 recursion ~0.18s << 1s).
- [ ] PiholeContainerDown stays `inactive` while `homelab_container_status{name="pihole-unbound",state="running"}` is present (the EPIC-003 `state` label vocabulary — `running` for a healthy container — is the rule-2 contract).
- [ ] Labels on all 22 rules: `severity` (info|warning|critical only) + `source_tool: vmalert-metrics` + `category: dns` + `integration: pihole`. PiholeDnsSplitDivergence lives in the `dns` group (Pi-hole is the subject); EPIC-016 owns the `path="direct"`-fails (WAN) rules over the shared `homelab_dns_resolution_*` family.

## STAGE-006-017 — vmalert-logs FTL alert rules

- [ ] `deploy/vmalert/logs/pihole.yaml` loads on the live `vmalert-logs` with all 4 rules `health=ok`: `docker exec homelab-monitor curl -s http://vmalert-logs:8880/api/v1/rules | jq '.data.groups[] | select(.name=="pihole_logs")'` — confirm `PiholeFtlRateLimit`, `PiholeFtlError`, `PiholeGravityUpdateFailedLog`, `PiholeDbMaintenanceAnomaly` all show `health: "ok"` and empty `lastError`.
- [ ] `PiholeFtlRateLimit` fires on a planted `Rate-limiting` FTL line and is silent at baseline (no `Rate-limiting` lines on `service:="pihole-unbound"` normally). Verify via `apps/monitor/tests/integration/test_vmalert_pihole_logs_pipeline.py::test_pihole_ftl_ratelimit_fires`.
- [ ] `PiholeFtlError` fires on a planted `ERROR:` line; silent on the many benign `WARNING:` baseline lines. Verify via `test_pihole_ftl_error_fires`.
- [ ] `PiholeGravityUpdateFailedLog` fires on a planted gravity whole-run-failure line AND does NOT false-fire on the recurring benign `was inaccessible during last gravity run` warning (the persistently-dead NRD blocklists). Baseline LogsQL count of the rule's failure phrases over `service:="pihole-unbound"` must be 0. Verify via `test_pihole_gravity_update_failed_fires` + baseline count.
- [ ] `PiholeDbMaintenanceAnomaly` fires on a planted DB-error line (e.g. `disk I/O error`) AND does NOT false-fire on the routine `Optimized database in X seconds` success line. Verify via `test_pihole_db_maintenance_anomaly_fires` + baseline count.
- [ ] All 4 rules carry labels `severity` (info/warning per rule) + `source_tool: vmalert-logs` + `target_kind: container` + `integration: pihole`.
- [ ] The 4 fire tests + the rules-load test pass in the integration rig: `bash scripts/run-integration.sh` includes `test_vmalert_pihole_logs_pipeline.py` green.
- [ ] FOLLOW-UP (inferred FTL phrasing): the `PiholeGravityUpdateFailedLog` whole-run-failure phrase set (`Unable to update gravity` / `Error: Unable to update gravity database` / `gravity failed`) and the `PiholeFtlRateLimit` `Rate-limiting` token are INFERRED from FTL v6 format, not observed in the live corpus (no real failures/rate-limits in the window). If a real gravity-run failure or rate-limit event ever lands, confirm FTL's actual wording matches the rule phrases and tighten if needed.

## STAGE-006-018 — Pi-hole write endpoints (blocking / gravity) + query-logging metric

- [ ] `POST /api/integrations/pihole/blocking` toggles blocking on the live Pi-hole: `{"action":"disable","confirm_phrase":"disable","timer":30}` → 200 `{"blocking":"disabled",...}`; `{"action":"enable","confirm_phrase":"enable"}` → 200 `{"blocking":"enabled",...}`. Each requires session+CSRF and writes an `audit_log` row (who/what=`pihole.blocking.{action}`/before/after). ALWAYS re-enable blocking after testing.
- [ ] `POST /api/integrations/pihole/gravity/update` `{"confirm_phrase":"update"}` → 200 `{"success":true,"log_tail":[...real pihole -g output...],"audit_id":...}`; the tail-parse correctly identifies success via the `[✓] Done.` marker; writes an audit row.
- [ ] Wrong `confirm_phrase` → HTTP 400 (`confirm_phrase must equal '<phrase>'`) BEFORE any client call (the body-only Depends validator runs before the RW-client dependency — must NOT return 503).
- [ ] Unauthenticated write → 401; valid session without `X-CSRF-Token` → 403 (csrf_mismatch). Both write endpoints are `Scope.PIHOLE_WRITE`-gated.
- [ ] `homelab_pihole_query_logging_enabled` (bare 1/0 gauge) emitted by the `pihole_config` collector from live `GET /api/config` `config.dns.queryLogging`; trigger via `POST /api/collectors/pihole_config/retry`, confirm in VictoriaMetrics. Fail-closed to 0.0 on error/missing field.
- [ ] `PiholeRestClient._get()` maps a 200-response carrying an `{"error":{"key":"unauthorized"}}` body to a re-auth+retry (same guard as HTTP 401); a non-unauthorized error key → `bad_response`. All 11 collectors still pass at 100% branch coverage.
- [ ] The RW client (`pihole_api_password_rw`) is used ONLY by the write router, never by collectors; constructed + aclose'd in lifespan.
- [ ] Live-3b confirmed (STAGE-006-018 Refinement): RW password authenticates against live Pi-hole v6; CSRF IS required on writes; real gravity run ~completes with the success markers; the blocking round-trip leaves the Pi-hole in its original state.

## STAGE-006-019 — generic container lifecycle actions (restart/start/stop)

- [ ] `POST /api/integrations/docker/containers/{name}/restart` restarts a live container: with `{"confirm_phrase":"restart"}` + session + CSRF → 200 `{action:"restart", container_name, container_id, audit_id}`; the container returns to RUNNING within the 30s write-timeout. (Validated live on `homelab-kthxbye`.) Equivalent `/start` + `/stop` endpoints exist. ALWAYS leave the container RUNNING after testing.
- [ ] Each lifecycle action writes an `audit_log` row: who, `what="docker.container.<action>"`, `before={"state":<pre-inspect status>}`, `after={"action":<action>}`, ip; the response `audit_id` matches the DB row.
- [ ] Wrong `confirm_phrase` → HTTP 400 (`confirm_phrase must equal '<action>'`) BEFORE the socket-client dependency (the body-only confirm validator runs first; must NOT 503/502).
- [ ] Unauthenticated → 401; valid session without `X-CSRF-Token` → 403 (csrf_mismatch); a token lacking `Scope.DOCKER_WRITE` → 403 (unit-tested). All three endpoints are `DOCKER_WRITE`-gated.
- [ ] Unknown container name → 404 (`container not found: <name>`); the socket method is never called.
- [ ] DockerSocketClient `restart_container`/`start_container`/`stop_container` raise `DockerSocketError` on failure (→ 502 at the router); success = HTTP 204 OR 304 (304 = already-in-state idempotent); the write methods use a 30s per-call timeout (longer than the 5s read default, for restart's stop-grace). `t` included only when `timeout_seconds` is passed.
- [ ] A pre-action `inspect_container` failure → 502 (the action is NOT attempted; audit not written).
- [ ] No audit row is written on the 400/401/403/404/502 paths (audit-on-success-only).
- [ ] EPIC-003.md carries a back-fill cross-reference note pointing to STAGE-006-019.

## STAGE-006-020 — Pi-hole panel data endpoints

- [ ] `GET /api/integrations/pihole/overview` (authed) returns 200 with real data: `up=true`, non-null `percent_blocked`, `query_frequency`, `messages_count`, `privacy_level`, `gravity_domains`, and a NON-EMPTY `versions[]` list (component/version pairs).
- [ ] `versions[]` in `/overview` is populated within ~1 minute of a monitor restart (the `pihole_version` startup-run hook in lifespan.py — Decision 3A; it does NOT wait for the 3600s interval).
- [ ] `GET /api/integrations/pihole/adlists` returns per-adlist rows with `status`/`enabled`/`domains` plus `gravity_domains` and `gravity_last_update_age_seconds`.
- [ ] `GET /api/integrations/pihole/upstreams` returns upstream rows (e.g. `127.0.0.1#5335`, `cache`, `blocklist`) with query counts.
- [ ] `GET /api/integrations/pihole/unbound` returns unbound cache stats (`cache_hit_ratio`, `queries_total`, etc.) or honest nulls when extended stats are disabled; `extended_stats_enabled` reflects reality.
- [ ] `GET /api/integrations/pihole/clients?count=5` (live RO Pi-hole call) returns real top clients with `returned` == row count; `?blocked=true` returns the blocked-query leaders (differs from the default).
- [ ] `GET /api/integrations/pihole/recent-blocked?count=5` and `/messages` (live RO calls) return real data; empty lists are valid/healthy states (no recent blocks / no FTL messages).
- [ ] Auth + validation: unauthenticated GET on any panel endpoint → 401; `/clients?count=999` → 422 (le=100); `/clients?count=0` → 422 (ge=1).
- [ ] All 7 panel paths appear in the served `/openapi.json` after a monitor image rebuild.
- [ ] VM-down behavior: VM-sourced endpoints (`/overview`,`/adlists`,`/upstreams`,`/unbound`) return 502 `upstream_unavailable` when VictoriaMetrics is unreachable; live endpoints return 502 on `PiholeError`, 503 when the RO Pi-hole client is uninitialized.

## STAGE-006-021 — Pi-hole panel shell + header status strip

- [ ] Navigating to Integrations → Pi-hole (sidebar entry, route `/integrations/pihole`) renders the panel shell with the header status strip + Overview/Logs tab nav.
- [ ] The header status strip shows live data from `GET /api/integrations/pihole/overview`: up/down badge, blocking state badge (on/off/—), block % (rounded to 1 decimal, e.g. "47.3% blocked"), queries/sec (rounded to 1 decimal), FTL-messages indicator (red badge "N messages" when count>0, else "No messages").
- [ ] When blocking is temporarily disabled (`blocking_enabled=false`, `blocking_timer_seconds>0`), the strip shows a live "re-enables in M:SS" countdown that ticks down each second and re-syncs to the server value on each 30s refetch (`useReenableCountdown` hook).
- [ ] `up=false` renders a critical "Pi-hole down" badge with other indicators as "—" (data state, not an error); a 502 from the endpoint shows the yellow "temporarily unavailable" banner; nullable fields render as "—".
- [ ] The Logs tab stays IN-PANEL: clicking it goes to `/integrations/pihole/logs` (Pi-hole sidebar entry stays active), rendering the "Coming soon (STAGE-006-024)" placeholder — it does NOT navigate to the global `/logs` page.
- [ ] The Overview tab shows placeholder sections for the future widgets (Blocking control / Gravity & adlists / Messages → 022; Upstreams & Unbound / Clients / Recent blocked / Version & container → 023).
- [ ] Desktop (1280×720) and Mobile (375×667): the status strip wraps cleanly on narrow viewports (flex-wrap); placeholder cards are full-width and readable with no overflow.
- [ ] The header auto-refreshes on the 30s TanStack Query `refetchInterval` (values update without a manual reload).

## STAGE-006-023 — Upstreams/unbound + clients (Tier 2) + recent-blocked + version widgets + container-control buttons

- [ ] Pi-hole panel Overview tab: "Upstreams & Unbound" section renders the upstreams table (upstream + queries) and an Unbound sub-card (cache-hit %, recursion p50/p95 in ms, DNSSEC secure/bogus, SERVFAIL, extended-stats badge); when extended stats disabled the 3 extended metrics hide with an "Extended stats off" note
- [ ] Backend `GET /api/integrations/pihole/unbound` returns the 5 extended fields (recursion_p50_seconds, recursion_p95_seconds, dnssec_secure_total, dnssec_bogus_total, servfail_total) populated from VM when extended_stats_enabled, null otherwise
- [ ] "Clients" section renders a merged top-talkers table (client, total queries, blocked, block%) joining the blocked=false and blocked=true /clients calls; block% guards divide-by-zero
- [ ] Clients table shows friendly Unifi device names: `GET /api/integrations/pihole/clients` enriches each row's `name` via time-windowed IP→MAC→Unifi-name join (Unifi name → hostname → Pi-hole name → null); IPs with no Unifi observation (e.g. container-internal IPs) correctly show the bare IP
- [ ] "Recent blocked" section renders the blocked-domain feed (honest empty state when none)
- [ ] "Version & container" section renders the versions/updates table (core/ftl/web/docker + update-available flags) and Restart/Start/Stop buttons for `pihole-unbound`
- [ ] Container-control buttons open a typed-confirm dialog (phrase = action word) and POST to `/api/integrations/docker/containers/pihole-unbound/{restart|start|stop}` with CSRF; wrong phrase → 400, missing CSRF → 403
- [ ] Clients table + all widgets responsive: real tables on sm+, stacked cards on mobile; no overflow/console errors (Desktop + Mobile)

## STAGE-006-025 — Tier-3 query-feed shipper (/api/queries → VictoriaLogs) + query_feed_streaming overview field + Logs-tab stream selector

- [ ] `GET /api/integrations/pihole/overview` includes `query_feed_streaming: bool` (config-derived from `pihole_stream_query_feed`; false in the public default)
- [ ] When `pihole_stream_query_feed` is OFF: the `pihole_query_feed` collector no-ops (ships nothing to VictoriaLogs); no `pihole-queries` stream data is produced
- [ ] When `pihole_stream_query_feed` is ON: the shipper pages `/api/queries` and writes structured JSON per-query lines to the `pihole-queries` VL stream (fields: query_id, domain, client_ip/name/kind, status, query_type, dnssec, reply_type/time, ede_code, upstream, cname, list_id; service=pihole-queries, source_type=pihole)
- [ ] Cursor dedup: subsequent ticks ship only id>last_id (durable high-water in app_settings `pihole.query_feed.last_id`); first run records a baseline and ships nothing; no duplicate re-ship across ticks
- [ ] Daily byte cap enforced (default 500 MiB, env `HOMELAB_MONITOR_PIHOLE_QUERY_FEED_MAX_BYTES_PER_DAY`); cap-hit drops + advances the cursor
- [ ] The query-feed stream is queryable via `/api/logs/query?services=pihole:pihole-queries` (monitor-written VL stream carries service/source_type fields so the existing services filter matches)
- [ ] `deploy/compose/docker-compose.yml` passes `HOMELAB_MONITOR_PIHOLE_STREAM_QUERY_FEED` through to the monitor container (default false) — the flag actually reaches the prod container
- [ ] Pi-hole Logs tab: when `query_feed_streaming` is true a segmented stream selector (Service log ↔ Query feed) appears; selecting Query feed swaps servicesCsv to `pihole:pihole-queries`, hides the errors-only toggle, defaults expr `*`; when false the selector is hidden (FTL-only). Desktop + Mobile.

## STAGE-006-024 — Embedded LogViewer (docker-stdout scoped) + Tier-3 query-feed view toggle (toggle deferred to 025)

- [ ] Pi-hole panel "Logs" tab renders the embedded `<LogViewer>` scoped to `docker:pihole-unbound` (FTL docker-stdout lines: gravity runs, rate-limits, API warnings, errors)
- [ ] The "Errors only" toggle defaults ON (LogsQL `WARNING OR ERROR OR CRITICAL`) and toggling OFF shows all lines (`*`); the errors-only result is a strict subset of all-lines (filter genuinely filters)
- [ ] LogViewer controls work: timezone toggle, line-wrap toggle, refresh, Open-in-Explorer deep-link (scoped to the pihole-unbound service), load-older pagination
- [ ] Honest empty/unavailable states (no_lines empty state; 502 → unavailable banner; generic error → no crash)
- [ ] Logs tab responsive: viewer + header controls usable on Desktop AND Mobile (no overflow, controls reachable)
- [ ] No Tier-3 per-query-feed toggle present yet (correctly deferred to STAGE-006-025; FTL docker-stdout view only)
