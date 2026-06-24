# EPIC-006: Pi-hole + Unbound integration

## Status: In Progress (current: STAGE-006-015 Complete; next: STAGE-006-016)

## Build order (IMPORTANT)

**EPIC-007 (Unifi) is built FIRST; EPIC-006 (Pi-hole) SECOND.** Epic numbers are unchanged; only
the build sequence is swapped (decided 2026-06-16 brainstorm). Rationale: Unifi is the authoritative
source of client identity (MAC↔IP↔device); Pi-hole can only reliably supply IP (see the client-join
contract below), so Unifi must own **client-object creation** and Pi-hole **consumes** those identities
to attach DNS behavior. The Unifi brainstorm (2026-06-17) has now RUN and **finalized the seam**:
**EPIC-007 OWNS and BUILDS the client view** (the persistent `unifi_clients` registry, the Network → Clients
tab, and the per-client Client page, plus a documented DNS-enrichment extension point). **EPIC-006 does NOT
build a separate merged view — it ENHANCES EPIC-007's Client page in place** with Pi-hole DNS behavior via
that extension point (see the rewritten STAGE-006-027). The build order was also amended to whole-epic
sequential **EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008**.

## Overview

Land Pi-hole + Unbound as a first-class integration bundle, mirroring EPIC-005 (Home Assistant) — the
exemplar integration-bundle epic. Full treatment: a real Pi-hole v6 REST client, a suite of collectors
(query stats / blocking / gravity+per-adlist / FTL health / FTL diagnostic messages / version / per-client
Tier-2 / unbound resolver / DNS health probes / DNS split-check), 26 default alert rules (metrics + logs),
a single Grafana dashboard tab, and an operator-facing Integrations → Pi-hole panel with **write-capable
controls** (enable/disable blocking, update gravity, container restart/start/stop). Pi-hole is one of the
most important services on this LAN — the firewall **forces** all clients onto it (see topology) — so
its failure is a real protection-loss / partial-outage event.

This epic **consumes** foundation already built by EPIC-004/005 and does NOT rebuild it: the
integration-bundle skeleton + registration pattern (005-003), the reusable cardinality cap (005-004), the
user-authored MetricsQL alert-rule machinery (005-005, so the user can add/tune Pi-hole metric thresholds
without code), the `<LogViewer>` embedding contract (004-003), Grafana-dashboards-as-code, and the
vmalert metrics+logs surfaces. Net: smaller than EPIC-005 was, but finer-grained than the original
6-stage sketch — "option A, right-sized."

## Source documents (read before starting any stage)

- Master design spec §2 (Q21 DNS-via-Pi-hole-and-direct split, integration_bundle model), §5 (plugin/
  collector/integration_bundle framework + Channel contract), §6.2 (`homelab_pihole_*` metric family),
  §9.2 (Integrations panel = plugin-provided panel; Metrics = Grafana embed), §3.4 (Pi-hole a discovered
  target).
- EPIC-005 (`epics/EPIC-005-home-assistant/`) — the exemplar; copy its wave shape + the verified code
  anchors it lists (integration bundle layout, cardinality cap, user-rule machinery, panel/router/sidebar
  registration, `useLogsQuery`, dashboards/rules paths). EPIC-005's `STAGE-005-003`..`042` are the closest
  structural precedent.
- `apps/ui/src/components/logs/README.md` — the `<LogViewer>` embedding contract (EPIC-004 STAGE-004-003).
- Project memory `reference_docker_inventory.md` — `pihole-unbound` (image `mpgirro/pihole-unbound`),
  `network_mode: host`, API on 8080, password in compose env, DNSSEC on, logs at
  `/storage/docker/pihole-unbound/logs`.

## Verified deployment reality (recon 2026-06-16 — re-verify live in each stage's Design)

- **Pi-hole v6.4.2** (Core 6.4.2 / FTL 6.6.2 / Web 6.5) in `mpgirro/pihole-unbound:latest`,
  `network_mode: host`, API at `http://localhost:8080`, DNSSEC enabled. Web + Docker updates currently
  available (good live test data for the update collector).
- **v6 REST API** (NO OpenAPI at this version): `POST /api/auth {"password":"..."}` → `session.sid`; send
  header **`X-FTL-SID: <sid>`** (the space after the colon is mandatory) on GETs; CSRF not needed for
  header auth; session validity 1800s, **each request extends it** (poll ≤5min to keep one session alive);
  `DELETE /api/auth` logs out. Re-auth on 401; back off on 429 (login is rate-limited). Reuse ONE session;
  do NOT auth-per-scrape (intermittent app-password bug + session-slot limit). Every response carries a
  `took` float = free API-latency signal.
- **Unbound** reachable via `docker exec pihole-unbound unbound-control stats_noreset` (unix control
  socket, no cert). It is Pi-hole's sole upstream at `127.0.0.1#5335`. **`extended-statistics` was ENABLED
  2026-06-16** (persistent host drop-in `/storage/docker/pihole-unbound/unbound.conf.d/99-extended-stats.conf`
  + a `:ro` bind-mount added to `/storage/docker/compose/docker-compose.yml` + container recreated), so the
  rich keys are now present: `num.answer.rcode.*`, `num.answer.secure`/`num.answer.bogus` (DNSSEC validation),
  `histogram.*` (recursion-time buckets), `num.query.type.*`. The collector MUST still **gracefully degrade**
  to the default key set if extended-stats is ever off (detect key presence; emit
  `homelab_pihole_unbound_extended_stats_enabled`).
- **Logs:** `FTL.log` lines ALSO appear on the container's stdout → already in VictoriaLogs via the EPIC-004
  vector docker pipeline. So **NO new log mount for Pi-hole**; the FTL-log alert rules run over the existing
  docker-stdout stream. The high-volume `pihole.log` query log (file-only, not stdout) is NOT ingested
  (redundant with FTL DB + the Tier-3 feed; privacy-heavy). DBs: `pihole-FTL.db` ~1.05 GB (~8d on disk),
  `gravity.db` ~332 MB (5.7M blocked domains). FTL currently reports **2 inaccessible-adlist `LIST` warnings**
  via `/api/info/messages` — real live test data for the per-adlist + messages signals.
- **DHCP:** Pi-hole is NOT the DHCP server (UDM is). No DHCP-leases collector/panel here — DHCP is EPIC-007.

## DNS topology (why Pi-hole-down matters) — AMENDED 2026-06-17 (Unifi brainstorm)

**The DNS steering mechanism is the per-network DHCP DNS handout, NOT a firewall redirect.** During the
Unifi brainstorm the user **deleted** the three legacy DNS-force firewall rules ("Pi-Hole Redirect to DNS",
"Drop DNS to UDM", and the one bypass rule) — they were already disabled and intentionally so. What actually
steers clients to Pi-hole today is the UDM's per-network DHCP setting `dhcpd_dns_1 = 192.168.2.148` (Pi-hole
handed out as the DNS server), with the LAN DNS-server list `192.168.2.148 → 1.1.1.1 → 8.8.8.8` as a
client-side fallback.

Net: a Pi-hole outage is a **silent protection loss**, NOT a hard partial-DNS-outage. Clients fail over to
the DHCP list's `1.1.1.1`/`8.8.8.8` and keep resolving — **but unfiltered** (no ad/tracker blocking), and
any client configured to ignore the handed-out DNS bypasses Pi-hole entirely. DNS keeps working; protection
silently stops. This is the rationale for the "down" alerts (loss of filtering for the whole LAN), and the
reason the **DNS-steering check that matters is the DHCP handout** — monitored by EPIC-007's
`UnifiDnsSteeringDrift` (`dhcp_dns_primary != 192.168.2.148`), not a firewall-rule check (the rules no longer
exist). EPIC-007 owns this DHCP-DNS-handout monitoring.

## Client-join contract (load-bearing — shared with EPIC-007)

- **Pi-hole's canonical client key is IP** (+ resolved hostname when known). MACs are present for only
  ~19% of active clients (Pi-hole in host-mode learns MACs from the host's ARP cache, which is sparse and
  ages out). Do NOT use MAC as the primary/sole join key.
- **Unifi is the identity authority** (reliable MAC↔current-IP for every associated client). The unified
  Network → Clients view joins **Pi-hole(IP-at-time-of-observation) → Unifi(IP→MAC→stable device)** —
  **time-windowed**, never a static IP→device map (IPs are DHCP-assigned and rotate). Pi-hole-supplied MAC
  (the 19%) is a corroborating secondary key only.
- **Reject** any scheme that tries to improve Pi-hole MAC coverage via host-side ARP population — Unifi is
  the better identity source; this is an explicit non-goal.
- **Loopback is KEPT, not dropped, and attributed to the host.** `127.0.0.1`/`::1`/`::`/`pi.hole` clients
  are the Pi-hole host's own DNS (the host `192.168.2.148` runs the monitor, Pi-hole, HA, Plex, Foundry,
  many host-mode containers — a top-tier actor). The collector tags them `client_kind` (`local` = host-app
  DNS, `resolver_self` = Pi-hole→unbound internal) and stamps the configured `pihole_host_lan_ip`
  (`192.168.2.148`) so they join onto the HOST device in the unified view. The host is a **first-class,
  high-importance device**, never excluded. Non-goal: Pi-hole physically cannot split loopback DNS by
  process/container (it only sees the source IP); we attribute to the host as a whole.

## Privacy tiers (decided)

- **Tier 1** (aggregate only) — not the default here.
- **Tier 2** (per-client + top-N, **live-queried** at view-time, capped + shorter-retention metrics) =
  **built-in default.** Top talkers, per-client query/block counts, top blocked/permitted domains,
  per-client drill-down. It's the user's own single-user homelab (all his devices), so the only cost is
  data-at-rest blast radius.
- **Tier 3** (full per-query feed `/api/queries` → VictoriaLogs) = **built, TOGGLEABLE, default-OFF in the
  public release, ON for this user** (`pihole_stream_query_feed`). Own VL stream cap + configurable
  retention. Enables forensic line-level search in the embedded LogViewer (incident timelines, malware/
  exfil DNS pattern hunting, per-query DNSSEC/EDE). PII-heavy by nature; deliberate retention.

## Metric families (all `homelab_pihole_*` / `homelab_unbound_*`, cardinality-capped)

Single reused RO-app-password session; `homelab_pihole_api_took_seconds{endpoint}` from every response.
**Skipped:** `/api/info/system` host CPU/mem + `/api/info/sensors` temps (duplicate node-level +
EPIC-005A-009 collectors — Pi-hole is on the monitor's own host). DHCP leases (UDM owns DHCP).

| Collector | Source | Cadence | Emits (abridged) |
|---|---|---|---|
| Core query stats | `/api/stats/summary` | 30s | queries/blocked/forwarded/cached, percent_blocked, query_frequency, unique_domains/clients, active/total_clients; `query_by_type{type}`/`query_by_status{status}`/`query_by_reply{reply}` |
| Upstreams | `/api/stats/upstreams` | 30s | `upstream_queries{upstream}` |
| Gravity + per-adlist | `/api/info/ftl`, `/api/lists` | 5m | `gravity_domains`, `gravity_last_update_age_seconds`, `adlist_domains{list}`/`adlist_enabled{list}`/`adlist_status{list}` |
| Blocking state | `/api/dns/blocking` | 30s | `blocking_enabled`(1/0), `blocking_timer_seconds` |
| FTL health | `/api/info/ftl`, `/api/info/database` | 60s | ftl_uptime/cpu_percent/memory_percent, db_size_bytes, db_queries_total, privacy_level, query_logging_enabled, dnsmasq cache counters |
| FTL messages | `/api/info/messages` | 60s | `messages_count`, `message{type}` (first-class) |
| Version/update | `/api/info/version` | 1h | `update_available{component}` (core/web/ftl/docker) |
| Per-client (Tier 2) | `/api/stats/top_clients` + network table | 60s | `client_queries{client_ip,client_name,client_kind}`, `client_blocked{...}`, `top_blocked_domain{domain}`, `top_permitted_domain{domain}` (capped ~top-50, shorter retention, loopback tagged+host-attributed) |
| Unbound | `unbound-control stats_noreset` (docker exec) | 60s | queries/cache_hits/cache_misses/cache_hit_ratio/prefetch, `recursion_time_seconds{quantile}` (avg/median + histogram p50/95/99), requestlist_current/exceeded, `answer_rcode{rcode}`, `answer_secure_total`/`answer_bogus_total` (DNSSEC), `query_type{type}`, `unbound_extended_stats_enabled` |
| DNS health probe | direct query to Pi-hole `:53` resolving `dns.google.com` | 60s | `homelab_pihole_up` (composite), `pihole_dns_probe_seconds` |
| DNS split-check | resolve via Pi-hole AND direct 1.1.1.1 | 60s | `homelab_dns_resolution_seconds{path}` (shared with EPIC-016) |
| Tier-3 query-feed shipper | `/api/queries` JSON → VictoriaLogs | streaming | per-query log stream (toggleable; a LOG shipper, not a metrics collector) |

(During Build, sweep live `/api/info/ftl` + `/api/stats/summary` for any additional useful counters and fold
them in — the set above is the comprehensive core, not necessarily exhaustive.)

## Alert catalog (26: 22 metrics + 4 logs) — severity vocab info|warning|critical

Anomaly rules use the project's rolling-baseline `clamp_min(K*avg_over_time(...))` idiom with warm-up;
absolute-threshold rules carry the load immediately. The user-authored-rule machinery (inherited from
005-005) also lets the user add/tune Pi-hole metric thresholds without code.

**Metrics rules (`deploy/vmalert/metrics/pihole.yaml`):**

| Alert | Condition | Severity |
|---|---|---|
| PiholeDown | `homelab_pihole_up == 0` | critical |
| PiholeDnsProbeFailing | direct `:53` probe fails | critical |
| PiholeContainerDown | EPIC-003 container not running/unhealthy | critical |
| PiholeDnsSplitDivergence | Pi-hole path fails but direct-1.1.1.1 succeeds | critical |
| PiholeBlockRateCollapsed | `percent_blocked`≈0 while volume normal | critical |
| PiholeUpstreamAllDown | all upstreams' share → 0 | critical |
| PiholeBlockingDisabled | `blocking_enabled==0` for >5m | warning |
| PiholeBlockingDisabledIndefinitely | `==0` AND no timer, **for >15m** | warning |
| PiholeUpstreamDown | one upstream's share → 0 | warning |
| PiholeGravityStale | `gravity_last_update_age > N days` (TZ-guarded) | warning |
| PiholeGravityDomainsDropped | `gravity_domains` dropped sharply (baseline) | warning |
| PiholeAdlistFailing | per-adlist status=failed >N | warning |
| PiholeMessagesPresent | `messages_count>0` (text in annotation) | warning |
| PiholeDbTooLarge | `db_size_bytes`>threshold / disk pressure | warning |
| PiholeClientFlooding | single client query-rate spike (baseline) | warning |
| UnboundDnssecBogusSpike | `answer_bogus_total` rate up | warning |
| UnboundServfailSpike | `answer_rcode{SERVFAIL}` rate up | warning |
| UnboundRecursionSlow | recursion-time p95 high | warning |
| PiholeUpdateAvailable | `update_available{component}==1` | info |
| PiholeBlockRateSpike | block% jumps (baseline) | info |
| PiholeApiSlow | `api_took_seconds` p95 high | info |
| UnboundCacheHitLow | cache-hit ratio drops (baseline) | info |

**Logs rules (`deploy/vmalert/logs/pihole.yaml`, over the existing docker-stdout stream):**

| Alert | Pattern | Severity |
|---|---|---|
| PiholeFtlRateLimit | FTL `RATE_LIMIT` line | warning |
| PiholeFtlError | FTL ERROR/WARNING lines | warning |
| PiholeGravityUpdateFailedLog | gravity-run failure lines | warning |
| PiholeDbMaintenanceAnomaly | DB-vacuum errors / unusual deletions | info |

The critical set = LAN-DNS-protection-loss (down / probe / container / split-divergence / block-collapse /
all-upstreams-down). NOTE (2026-06-17): "protection loss" is the accurate framing — with the firewall
DNS-force rules deleted, a Pi-hole outage is a *silent loss of ad/tracker filtering* (clients fail over to
the DHCP fallback `1.1.1.1`/`8.8.8.8` and keep resolving, unfiltered), NOT a hard DNS outage. The criticals
still warrant paging (the whole LAN loses filtering), but the rationale is protection-loss, not
connectivity-loss. The DHCP-DNS-handout drift detector lives in EPIC-007 (`UnifiDnsSteeringDrift`), not here.
The "down" alert should ideally route via a path independent of Pi-hole's own DNS
(HA push / EPIC-014 watchdog backstop); pinning the monitor's own resolver to a direct upstream so alerts
escape a DNS outage is NOTED as a requirement but its hardening lives in EPIC-014, not here.

## Operator panel (Integrations → Pi-hole) + write controls

Reuses the EPIC-005 panel pattern (shell + sidebar/router registration + per-widget). Header status strip:
up/down · blocking state + live auto-re-enable countdown · block% · q/s · FTL-messages indicator. Widgets:
1. **Blocking control** — state+timer + actionable enable/disable (write; confirm + audit).
2. **Gravity / blocklists** — count, last-update age, per-adlist health table + "Update gravity now" (write).
3. **FTL diagnostic messages** — live `/api/info/messages` list.
4. **Upstreams & resolver** — upstreams table + Unbound sub-card (cache-hit ratio, recursion p50/p95, DNSSEC
   secure/bogus, SERVFAIL rate, extended-stats-enabled indicator).
5. **Clients (Tier 2)** — top talkers (host shown as top device), per-client drill-down (live-query at
   view-time, reusing the app's refetch-interval — no stored history for the drill-down).
6. **Recent blocked feed** — live stream.
7. **Version / updates** — per-component versions + update-available.
8. **Privacy banner** — shown ONLY if privacy level elevated.
9. **Container control** — restart/start/stop `pihole-unbound` (via the generic action below; confirm + audit).
10. **Embedded `<LogViewer>`** — scoped to the Pi-hole container's docker-stdout `service` label; + Tier-3
    per-query-feed view toggle when enabled.

**Write-action backend:** behind `require_user_or_token` + confirm-on-destructive + `audit_log`, using the
**write-scoped app password** (`pihole_api_password_rw`): `POST /api/integrations/pihole/blocking`,
`POST /api/integrations/pihole/gravity/update`.

**Generic container-lifecycle backfill (EPIC-003 back-fill, Pi-hole-agnostic):**
`POST /api/docker/containers/{id}/{restart|start|stop}` — confirm-gated + audited, surfaced on ANY
container's inventory/detail view; the Pi-hole panel consumes it for `pihole-unbound`. Lives in the shared
Docker/inventory code (just authored here). A cross-reference note is added to EPIC-003. Scope: restart +
start + stop.

## Grafana dashboard (single `deploy/grafana/dashboards/pihole.json`, embedded in Metrics tab)

Seven collapsible rows, top = glance-first: (1) Health & blocking, (2) Query volume over time, (3) Query
composition (type/status/reply/upstream), (4) Gravity / blocklists (+ per-adlist table), (5) Clients
(Tier 2, host shown), (6) Unbound (cache/recursion p50/95/99 + DNSSEC secure/bogus + RCODE), (7) FTL/process
& API (uptime/CPU/mem/DB/privacy/query-logging/API-took). A readability-review pass stage ensures it reads
well against live data. Grafana stays on AGGREGATE metrics — no query-feed panels (those are LogViewer /
EPIC-020 territory).

## Credentials & config (defaults are open-source-safe; user override supplies real values)

- **Two least-privilege app passwords** (generated in Pi-hole Settings → API): `pihole_api_password_ro`
  (collectors — reads only) and `pihole_api_password_rw` (panel control actions). Collector code only ever
  calls read endpoints (the real protection — note a Pi-hole quirk where RO app passwords are not a hard
  server-side boundary). The user's main Pi-hole password is NOT stored by the monitor. Document the exact
  app-password generation steps + the two secret names in the integration README. Do NOT touch the user's
  live password.
- **Config (in plugin config, defaults shown):** `pihole_url` (`http://localhost:8080`),
  `pihole_stream_query_feed` (`false` public / `true` user), `pihole_host_lan_ip` (`192.168.2.148` user;
  empty public → loopback shown as host-unattributed), per-client cardinality caps (~top-50), poll
  intervals, unbound access = docker-exec (reuses the existing Docker socket from EPIC-003; no new Pi-hole
  config, no unbound TCP exposure).

## Scope-outs (deliberately NOT in this epic)

Host CPU/mem/temps (dup existing collectors); DHCP leases (UDM/EPIC-007); modifying adlists/allow/deny/
groups/regex/local-DNS records from our UI (Pi-hole-admin territory — panel is observe + the action buttons,
not an admin replacement); Pi-hole failover/HA-pair (single instance); discovery (consumes the
already-discovered target; "Pi-hole sees a new client" → EPIC-011/007). Claude auto-fix runbook CONTENT
(`pihole-restart`, `pihole-gravity-update`) → authored in EPIC-009 (engine + `homelab-fixer` user live
there); listed here only as candidate runbooks for EPIC-009. Aggregate log heatmaps (query-feed hour-of-day,
NXDOMAIN/SERVFAIL-by-domain, etc.) → EPIC-020 STAGE-020-009 (already recorded there).

## Stage decomposition (26 stages, sequential within waves)

Each stage lands a single small slice and ships independently usable, mirroring EPIC-005's wave shape.

### Wave A — Foundation (4)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-001 | Pi-hole v6 client + RO/RW app-password secrets + lifespan wiring + smoke (`/api/info/version`); pin the vector `service` label for `pihole-unbound` |
| STAGE-006-002 | `integrations/pihole/` bundle skeleton + registration (mirror 005-003) |
| STAGE-006-003 | Unbound-control access layer (docker-exec `stats_noreset` parser + extended-stats detection / graceful-degrade) |
| STAGE-006-004 | Per-client cardinality + loopback-attribution helper (`client_kind` tagging + `pihole_host_lan_ip` mapping) |

### Wave B — Collectors (8)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-005 | Core query-stats collector (summary: totals/%/freq + by-type/status/reply) |
| STAGE-006-006 | Upstreams collector |
| STAGE-006-007 | Gravity + per-adlist health collector |
| STAGE-006-008 | Blocking-state collector |
| STAGE-006-009 | FTL health + DB collector |
| STAGE-006-010 | FTL diagnostic-messages collector |
| STAGE-006-011 | Version/update collector |
| STAGE-006-012 | Per-client (Tier 2) collector (top-N, capped, loopback-attributed) |

### Wave C — Resolver & DNS health (3)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-013 | Unbound stats collector (cache/recursion/requestlist + DNSSEC secure/bogus + RCODE + per-type) |
| STAGE-006-014 | DNS health probe (`homelab_pihole_up` composite + direct `:53` probe of `dns.google.com`) |
| STAGE-006-015 | DNS split-check collector (Pi-hole vs direct-1.1.1.1; shared with EPIC-016) |

### Wave D — Alert rules (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-016 | vmalert-METRICS rules (the 22 metric alerts) |
| STAGE-006-017 | vmalert-LOGS rules (the 4 FTL-log alerts over docker-stdout) |

### Wave E — Write actions (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-018 | Pi-hole write endpoints (blocking enable/disable, gravity-update) — RW credential, confirm + audit |
| STAGE-006-019 | Generic container lifecycle actions (restart/start/stop, confirm + audit, any-container) — **EPIC-003 back-fill** |

### Wave F — UI panel (5)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-020 | Backend panel data endpoint(s) |
| STAGE-006-021 | Panel shell + sidebar/router registration + header status strip |
| STAGE-006-022 | Blocking control + gravity/adlist + FTL-messages widgets (incl. write buttons + privacy banner) |
| STAGE-006-023 | Upstreams/unbound + clients (Tier 2 drill-down) + recent-blocked + version widgets + container-control buttons |
| STAGE-006-024 | Embedded `<LogViewer>` (docker-stdout scoped) + Tier-3 query-feed view toggle |

### Wave G — Tier 3 + Grafana (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-025 | Tier-3 query-feed shipper (`/api/queries` → VL stream; toggleable default-off/on-for-user; stream cap + retention) |
| STAGE-006-026 | Grafana `pihole.json` dashboard + Metrics-tab embed + readability review pass |

### Deferred (cross-epic — finalized in the Unifi brainstorm, NOT counted in the 26)
| # | Stage | Theme |
|---|---|---|
| STAGE-006-027 | **Enhance EPIC-007's Client page + Clients-tab with Pi-hole DNS behavior** (finalized 2026-06-17). EPIC-007 OWNS and BUILDS the client view (registry + Network→Clients tab + per-client Client page + a documented DNS-enrichment extension point). This stage does NOT build a separate merged view — it ENHANCES EPIC-007's existing Client page in place, plugging DNS behavior (query volume / block rate / top domains / recent blocks / DNSSEC) into EPIC-007's extension point, joined time-windowed by IP→MAC; loopback→host. Built AFTER EPIC-007 lands (hard dependency). See the rewritten STAGE-006-027.md. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:
- **The "is Pi-hole alive" check NEVER routes through Pi-hole** — the DNS probe queries Pi-hole's listener
  directly, and the split-check uses a direct upstream (1.1.1.1) so Pi-hole-broken vs WAN-broken is
  distinguishable. (Same circular-dependency rule as EPIC-014/016.)
- **Tier 2 is the default; client IPs/domains are the user's own data** — Tier 3 (full query feed to VL) is
  toggleable, default-OFF public / ON for this user, with a VL stream cap + deliberate retention.
- **Loopback DNS is kept and attributed to the host `192.168.2.148`** (never dropped); the host is a
  first-class device in the unified view.
- **Least-privilege credentials** — collectors use the RO app password and only call read endpoints; the RW
  app password is used solely by the confirm-gated + audited write endpoints.
- **No new log mounts** — FTL-log alerts run over the existing docker-stdout stream; raw `pihole.log` is not
  ingested.
- **Collectors degrade gracefully** — unbound rich-vs-default stat set; cardinality caps prevent client
  explosion; emit `homelab_collector_run_*` self-metrics (kernel-provided).

## Dependencies

- EPIC-001 (kernel).
- EPIC-003 (Docker collector — container-down signal; and this epic back-fills the generic container
  lifecycle actions into the shared Docker surface).
- EPIC-004 (`<LogViewer>` embed contract; log rules over the docker-stdout stream).
- EPIC-005 (integration-bundle skeleton/registration, cardinality cap, user-authored-rule machinery, panel/
  router/sidebar pattern; HA push routing for critical Pi-hole alerts = recommended config, not strict).
- **EPIC-007 (Unifi) — built FIRST.** Owns client-object creation; EPIC-006 consumes Unifi client identities
  for the unified Network → Clients view (deferred STAGE-006-027).
- EPIC-016 shares the DNS split-check (`homelab_dns_resolution_seconds{path}`).

## Notes

- Build sequence is whole-epic sequential EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008 (2026-06-16/17
  brainstorms); numbers unchanged. The Unifi brainstorm has RUN and amended two decisions here: (1) the
  client-view seam — EPIC-007 builds the client view, EPIC-006 ENHANCES it (STAGE-006-027 rewritten); (2)
  the Pi-hole-down severity rationale — the firewall DNS-force rules were deleted, so DNS steering is the
  DHCP handout and a Pi-hole outage is a *silent protection loss*, not a hard DNS outage (DNS-topology +
  alert-catalog sections amended).
- Unbound `extended-statistics` was enabled on the live host during this brainstorm (persistent drop-in +
  compose bind-mount + recreate). The user's compose now has one extra `:ro` volume line on `pihole-unbound`.
- Candidate Claude auto-fix runbooks for EPIC-009 to author later: `pihole-restart` (risky — DNS blip),
  `pihole-gravity-update` (retry transient adlist failures). The common "blocking left off" fix is the panel
  button, not a runbook.
- The user leans toward MORE alerts / MORE detail — reflected in the 26-alert catalog and the per-adlist /
  per-client granularity.
