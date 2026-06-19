# EPIC-007: Unifi integration

## Status: In Progress (STAGE-007-001..013 Complete; current: STAGE-007-014)

## Build order + client-identity ownership (LOCKED — 2026-06-16/17 brainstorm)

**Whole-epic build sequence: EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008** (epic numbers unchanged; only
build order is sequenced). Rationale chain:

- **EPIC-017 (SSH probe framework) is built FIRST** so the per-target scoped-user + forced-command
  framework exists before any epic ships an SSH collector. EPIC-007's opt-in DHCP-lease collector and
  EPIC-008's DSM probes are built ON that framework — no "unscoped now, harden later" debt. (See the
  EPIC-017 banner: re-decompose from scratch; includes an `uptime` exemplar probe against BOTH the UDM and
  the Synology; mandate to replace the currently-unscoped SSH paths with scoped forced-command users.)
- **EPIC-007 (Unifi) is built SECOND.** Unifi is the **authoritative source of client identity** (reliable
  MAC↔current-IP↔stable device for every associated client — confirmed live: `stat/sta` returns full
  identity for all active clients). Pi-hole can only reliably supply IP (~19% MAC coverage), so **EPIC-007
  OWNS client-object creation**: the persistent `unifi_clients` registry (MAC-keyed, time-stamped IP↔MAC
  observations), the **Network page**, the **Clients tab**, and the **per-client Client page**.
- **EPIC-006 (Pi-hole) is built THIRD and ENHANCES EPIC-007's Client page + Clients-tab rows** with Pi-hole
  DNS behavior (query volume, block rate, top domains, recent blocks, DNSSEC/SERVFAIL), joined
  time-windowed by IP→MAC onto the registry EPIC-007 persisted. EPIC-006 does NOT build its own client
  view — it plugs into EPIC-007's documented **DNS-enrichment extension point**. (STAGE-006-027 is rewritten
  to this "enhance, don't rebuild" shape.)
- **EPIC-008 (Synology) is built FOURTH**, on the EPIC-017 SSH framework.

**Client-join contract (load-bearing — shared with EPIC-006):**
- Join is **time-windowed by IP-at-time-of-observation**, NEVER a static IP→device map (DHCP IPs rotate).
  Unifi is the stable identity anchor; Pi-hole's IP is the lookup key into the `unifi_clients` registry.
  Pi-hole-supplied MAC (the ~19%) is a corroborating secondary key only.
- **The host `192.168.2.148` is a FIRST-CLASS, top-tier device** in the registry and every view (it runs the
  monitor, Pi-hole, HA, Plex, Foundry, many host-mode containers). Never hidden/excluded; it has its own
  Client page.
- The persistent registry is the canonical client inventory the EPIC-006 join, the digest "what changed"
  section, and `UnifiNewClient` detection all read.

## Overview

Land Unifi as a first-class integration bundle, mirroring EPIC-005 (Home Assistant) — the exemplar
integration-bundle epic. The live recon (2026-06-16/17, read-only) **changed the integration model from the
spec's §2 Q11 assumption** (see Amendments): a single **read-only API key** authenticates BOTH the official
`v1` Integrations API AND the classic reverse-engineered API on this firmware, delivering the **entire
metric surface directly** — so we **drop `unpoller`** and use **direct-API collectors only** for metrics.

Full treatment: a real Unifi API client (key auth, classic + v1), ~10 collectors (combined device /
WAN+speedtest / client-identity / per-client stats + WiFi-experience / per-client DPI / alarms / DHCP /
controller-health / Teleport-VPN / opt-in SSH lease), a persistent MAC-keyed client registry, ~24 metric +
~5 log default alert rules, a CEF-syslog→VictoriaLogs event pipeline, **two** Grafana dashboards
(`unifi.json` gear-centric + `network.json` network-centric), and a UI restructure: remove the Inventory
page, move everything under **Integrations ▸**, add **Unifi** and **Network** pages plus a per-client
**Client page**. **Observe-only** — the API key is read-only; no write actions this epic.

This epic **consumes** foundation already built by EPIC-001/003/004/005 and does NOT rebuild it: the
integration-bundle skeleton + registration pattern (005-003), the reusable cardinality cap (005-004), the
user-authored MetricsQL alert-rule machinery (005-005), the `<LogViewer>` embedding contract (004-003), the
vector→VL pipeline (EPIC-004), Grafana-dashboards-as-code, and the vmalert metrics+logs surfaces.

## Amendments to the master spec / prior epics (recorded here, applied when files are written)

1. **Spec §2 Q11 (Unifi integration paths) amended:** the spec assumed a triad — `unpoller` Prometheus
   exporter (metrics) + UDM syslog (events) + direct UDM API (gap-fill). The live recon proved the
   **read-only API key alone** supplies the full metric surface (`stat/device` fat records: ports/PoE/
   radios/PDU/temp; `stat/sta` identity + per-client stats; `stat/health` WAN/speedtest; `stat/dpi`;
   `rest/networkconf`; `rest/alarm`). **We drop `unpoller` entirely** and write direct-API collectors
   (`homelab_unifi_*`), one `unifi` concurrency group. Syslog (CEF) is still used — but for the raw EVENT
   stream into VL, not metrics. This is "Option A — drop unpoller; direct-API for everything."
2. **Spec §9.1 nav amended:** the top-level **Inventory** page is removed; **Crons** moves under
   **Integrations ▸** (relocated from Inventory — cross-epic UI move, EPIC-002 provenance); **Pi-hole,
   Synology, Unifi, Network** all live under **Integrations ▸** alongside Home Assistant and Docker. A new
   top-level **Network** concept lives as an Integrations entry. (Recorded as an intentional deviation.)
3. **EPIC-006 Pi-hole-down severity rationale amended:** during this brainstorm the user **deleted** the
   three DNS-force firewall rules ("Pi-Hole Redirect to DNS", "Drop DNS to UDM", the Surefeed bypass) — they
   were already disabled and intentionally so. DNS steering now happens via the **per-network DHCP DNS
   handout** (`dhcpd_dns_1=192.168.2.148`), NOT a firewall redirect. So Pi-hole-down is a **silent
   protection-loss** (clients fail over to the DHCP list `1.1.1.1`/`8.8.8.8`, unfiltered) — NOT a hard
   partial DNS-outage. EPIC-006's topology section + severity wording are updated accordingly; the
   firewall-rule details are removed (the rules no longer exist).

## Source documents (read before starting any stage)

- Master design spec §2 (Q11 Unifi — AMENDED above; integration_bundle model), §3.1 (`CollectorContext`,
  `SshClientFactory`), §3.4 (UDM + switches/APs/PDU discovered targets), §5 (plugin/collector/
  integration_bundle framework), §6.1 (`targets`/registry tables), §6.2 (metric families), §9.1/§9.2
  (nav — AMENDED above; Integrations panel = plugin-provided panel; Metrics = Grafana embed).
- EPIC-005 (`epics/EPIC-005-home-assistant/`) — the exemplar; copy its wave shape + verified code anchors
  (integration bundle layout, cardinality cap 005-004, user-rule machinery 005-005, panel/router/sidebar
  registration, dashboards/rules paths).
- EPIC-006 (`epics/EPIC-006-pihole/`) — the sibling bundle; EPIC-006 enhances THIS epic's Client page.
- `apps/ui/src/components/logs/README.md` — the `<LogViewer>` embedding contract (EPIC-004 STAGE-004-003).
- Project memory `reference_homelab_inventory.md` — UDM, USP PDU Pro, USW-48-PoE, 2× USW Flex,
  USW-Lite-16-PoE, 2× U7-Pro-Wall; controller runs on the UDM.

## Verified deployment reality (recon 2026-06-16/17 — re-verify live in each stage's Design)

- **UDM Pro**, UniFi OS core **5.1.15**, **Network application 10.4.57**, LAN `192.168.2.1`, self-signed TLS
  (`CN=unifi.local`, no CA chain → client uses `verify=false` / pins this cert). Site id internalReference
  `default` (classic path `s/default`). 8 adopted devices, 56 active clients, 64 DHCP leases.
- **Single read-only API key** (Settings → Control Plane → Integrations → create API key). **There is no
  separate write key — the key is read-only by nature.** It authenticates BOTH:
  - **Official v1:** `GET /proxy/network/integrations/v1/sites`, `/sites/{id}/clients`, `/sites/{id}/devices`,
    `/devices/{id}`, `/devices/{id}/statistics/latest`. (Both `integration` and `integrations` spellings
    work on this firmware.) Gives device inventory + CPU/mem/uptime/load + coarse client MAC↔IP↔name — but
    NOT hostname, NOT client→AP/port binding, NOT PoE/outlet/radio stats. **Insufficient alone.**
  - **Classic (reverse-engineered):** `GET /proxy/network/api/s/default/<ep>` with the same `X-API-KEY`
    header returns 200. This is the workhorse: `stat/device` (FAT: `port_table` PoE/link, PDU `outlet_table`,
    AP `radio_table_stats` airtime/satisfaction, UDM `temperatures`), `stat/sta` (full client identity:
    `mac/ip/hostname/name/oui/network/last_seen/uptime`; wired→`sw_mac/sw_port/use_fixedip/fixed_ip`;
    wireless→`ap_mac/essid/channel/radio/signal`), `stat/health` (`www` block: `speedtest_*`,
    `xput_down/up`, `latency`, `drops`), `rest/networkconf` (DHCP ranges, `dhcpd_dns_*`, reservations),
    `rest/alarm?archived=false` (IDS/IPS/threats), `stat/dpi` (per-app), `stat/sysinfo` (version).
    `stat/event` is POST-only → we use syslog for the raw event stream instead (stay GET-only).

  **Two site identifiers (verified live, STAGE-007-001):** the classic API path `/proxy/network/api/s/{site}/` requires the SHORT site NAME (`"default"`), while the v1 Integrations site-scoped paths `/sites/{id}/` require the site UUID (resolved from `v1/sites` as `data[0].id`). These are DIFFERENT identifiers — the classic API returns 401 if given the v1 UUID. The `UnifiClient` exposes both `site_name` (classic) and `v1_site_id` (v1 UUID); Wave B/C collectors must use the correct one per surface.
- **NO local Network account needed.** The key covers identity, device/port/PoE/outlet/radio stats, DHCP
  config, alarms, health, speedtest, DPI. (`unpoller` would have needed a username/password account — another
  reason we dropped it.)
- **DHCP lease table (with expiry):** authoritative only from the lease file on the UDM at
  `/data/udapi-config/dnsmasq.lease` (standard dnsmasq 5-field: `<expiry-epoch> <mac> <ip> <hostname|*>
  <clientid>`). Read via an **opt-in, default-OFF, SSH-based collector built on the EPIC-017 framework**.
  This is pure **enrichment** (lease-expiry + lease-only devices); the API `stat/sta` is the primary
  identity source and the epic is fully functional API-only without it.
- **DNS steering:** per-network DHCP handout `dhcpd_dns_1=192.168.2.148` (Pi-hole). The three legacy
  DNS-force firewall rules were deleted (see Amendment 3). So the **DHCP-DNS-handout is the sole DNS-steering
  mechanism** to watch (`UnifiDnsSteeringDrift`).
- **Syslog:** NOT configured yet. UDM emits **CEF-over-syslog** (NOT plain RFC5424 — strict parsers reject
  it). vector has no syslog source today; we ADD a CEF-aware `syslog` source and the user enables UDM remote
  syslog (a UDM-side write, done AT the syslog stage, not before).
- **SNMP:** snmpd present but NOT running on the UDM; SNMP is effectively dead on UniFi OS → not used.
- **Teleport/VPN:** user uses WiFiman **Teleport** for remote access. Exact API field shape confirmed live at
  the Teleport stage's Design (recon didn't drill the vpn block).

## Credential / transport model (LOCKED)

- **`unifi_api_key`** in the encrypted secrets store (single credential; read-only; no RO/RW split). The
  integration README documents creating it (Settings → Control Plane → Integrations) and that it is
  read-only. Used ONLY on GET requests.
- **Observe-only epic** — NO Unifi write actions (no device restart, no PoE-port cycle, no PDU-outlet cycle).
  Power-cycling network gear is high-blast-radius and not needed for the monitoring mission (YAGNI). A future
  epic can add confirm-gated actions if ever wanted.
- **One `unifi` concurrency group** for every collector hitting the controller (never DDoS it).
- **SSH transport (opt-in, default-OFF)** — only the DHCP-lease collector. Built on the EPIC-017 scoped
  forced-command framework. **No committed doc states that a passwordless-root SSH trust to the router
  currently exists**; the config option is worded neutrally ("optional SSH lease read; requires SSH access
  you configure; disabled by default"). The mandate to replace the currently-unscoped path with a scoped
  user lives in EPIC-017.

## Metric families (all `homelab_unifi_*`, cardinality-capped, single `unifi` group, API key, observe-only)

`homelab_unifi_api_took_seconds{endpoint}` emitted from every response (free latency signal).

| Collector | Source | Cadence | Emits (abridged) |
|---|---|---|---|
| Combined device | `stat/device` (one fetch) | 60s | `device_up{device,model,kind}`, state, `firmware_info`, `update_available`, uptime, cpu%/mem%/load, temp; **ports**: link/`port_speed_bps`, `poe_power/current/voltage`, `poe_good`, errors/drops, `mac_table_count`; **AP radios**: `cu_total/self_rx/self_tx` (airtime), `num_sta`, `tx_power`, `tx_retries_pct`, `satisfaction`, channel/bw; **PDU**: `outlet_relay_state{outlet}`, outlet name (NO power — HA owns USP PDU Pro wattage) |
| WAN / ISP + speedtest | `stat/health` (`www`) | 30s | `wan_up`, `wan_latency_seconds`, `wan_drops`, `wan_xput_down/up`, `speedtest_download/upload/ping`, `speedtest_lastrun`, `wan_failover_active` |
| Active-client identity | `stat/sta` (+ `stat/alluser` for known) | 60s | upserts the persistent `unifi_clients` registry (MAC↔IP↔hostname↔ap_mac/sw_port↔fixed_ip; online/offline) |
| Per-client stats + WiFi-experience | `stat/sta` | 60s | `client_signal_dbm`, `client_tx/rx_rate_bps`, `client_uptime`, bytes; experience rollups (counts of clients below signal/satisfaction thresholds, high retries) — capped |
| Per-client DPI | `stat/dpi` | 5m | `client_dpi_bytes{client,app,cat}` per-client-per-app (cardinality-capped top-N×top-N + clamp for known counter spikes) |
| Alarms / threats | `rest/alarm?archived=false` | 60s | `threat_count`, `threat{type}` (drives `UnifiThreatDetected`; the only threat ALERT path) |
| DHCP config + DNS-steering | `rest/networkconf` | 5m | `dhcp_pool_size`, `dhcp_dns_primary` (→ DNS-steering-drift), reservation count, pool range (→ pool-exhaustion) |
| Controller-up + API latency | composite | 30s | `homelab_unifi_up`, `homelab_unifi_api_took_seconds{endpoint}` |
| VPN / Teleport | `stat/health` vpn + Teleport fields | 60s | `teleport_sessions`, `teleport_up` (field names confirmed live at Build) |
| SSH DHCP-lease (opt-in, default-OFF, on EPIC-017 framework) | UDM lease file | 5m | `dhcp_lease_count`, per-lease `lease_expiry` → **enriches** registry rows (never the identity source) |

(During Build, sweep live `stat/device` / `stat/sta` / `stat/health` for any additional useful fields and
fold them in — the set above is the comprehensive core, not necessarily exhaustive.)

## Persistent client registry (the canonical client inventory EPIC-006 enhances)

A new SQLite table (Alembic migration) `unifi_clients`, **keyed by MAC** (stable identity), holding: current
IP, hostname/name, device type/oui, network/SSID, AP-mac/switch-port, `fixed_ip`/`use_fixedip`,
online/offline, first_seen, last_seen, plus **time-stamped IP↔MAC observations** (a satellite table or
JSON-history column) so EPIC-006's join is **time-windowed**, never a static map. The host `192.168.2.148`
is a first-class row. Sourced from `stat/sta` (active) + `stat/alluser` (known). The opt-in lease collector
adds `lease_expiry`. This is what the EPIC-006 DNS-enrichment, the digest "what changed", and
`UnifiNewClient` read.

## Alert catalog (~24 metrics + ~5 logs) — severity vocab info|warning|critical

Anomaly rules use the project's rolling-baseline `clamp_min(K*avg_over_time(...))` idiom with warm-up;
absolute-threshold rules carry the load immediately. The 005-005 user-authored-rule machinery lets the user
tune thresholds without code.

**Metrics rules (`deploy/vmalert/metrics/unifi.yaml`):**

| Alert | Condition | Severity |
|---|---|---|
| UnifiControllerDown | `homelab_unifi_up == 0` | critical |
| UnifiWanDown | `stat/health` www down / WAN uplink lost | critical |
| UnifiGatewayDown | the UDM device unreachable | critical |
| UnifiDeviceDown | any adopted AP/switch/PDU `state != connected` (per-device, names which) | critical |
| UnifiDnsSteeringDrift | `dhcp_dns_primary != 192.168.2.148` (silent protection-loss; sole DNS detector) | critical |
| UnifiDeviceFlapping | disconnect/reconnect count spike (baseline) | warning |
| UnifiSwitchPortErrors | per-port error/drop rate elevated (baseline) | warning |
| UnifiPoePortFault | `poe_good==0` on a powering port / PoE draw anomaly | warning |
| UnifiApHighChannelUtil | `cu_total` sustained high (RF congestion) | warning |
| UnifiApLowSatisfaction | AP/VAP `satisfaction` low sustained | warning |
| UnifiClientPoorExperience | count of clients below signal / high retries (rollup) | warning |
| UnifiWanLatencyHigh | WAN `latency` elevated (baseline) | warning |
| UnifiWanPacketLoss | WAN `drops` elevated | warning |
| UnifiThreatDetected | new IDS/IPS alarm (structured `rest/alarm` path — the only threat alert) | warning |
| UnifiDeviceHighTemp | UDM/switch temperature high | warning |
| UnifiDeviceHighCpuMem | device cpu% or mem% sustained high | warning |
| UnifiTeleportDown | Teleport/VPN unhealthy while expected up | warning |
| UnifiDhcpPoolExhaustion | lease/active count approaching pool range size | warning |
| UnifiSwitchUplinkSaturation | uplink rx/tx approaching `port_speed_bps` | warning |
| UnifiFirmwareUpdateAvailable | `update_available{device}==1` | info |
| UnifiNewClient | never-before-seen MAC associated (→ EPIC-011 upgrades to a discovery/suggestion) | info |
| UnifiSpeedtestDegraded | speedtest down/up dropped vs baseline | info |
| UnifiApiSlow | `api_took_seconds` p95 high | info |
| UnifiClientCountAnomaly | active-client count sharp drop/spike (mass-disconnect signal) | info |
| UnifiSsidClientCountAnomaly | per-SSID client count sharp change | info |

**Logs rules (`deploy/vmalert/logs/unifi.yaml`, over the CEF syslog→VL stream):**

| Alert | Pattern | Severity |
|---|---|---|
| UnifiPortFlapLog | port up/down flap lines | warning |
| UnifiDeviceDisconnectLog | AP/gateway disconnect lines | warning |
| UnifiFirmwareEventLog | firmware update/apply lines | info |
| UnifiAdminLoginLog | admin login lines | info |
| UnifiThreatLog | IDS/IPS detection lines — **forensics/LogViewer only, NO alert** (the alert fires off the structured alarm path, so no double-page) | info |

## Events pipeline (CEF syslog → vector → VictoriaLogs)

vector gets a new **CEF-aware `syslog` source** (UDP/TCP; non-privileged port to avoid root-bind), a VRL
transform extracting CEF fields (`UNIFIcategory`/`UNIFIsubCategory`/`src`/`msg`/signature-id), writing to VL
with **`service="udm-<category>"`** labels so the per-`service` Drain models (EPIC-004 STAGE-004-025+)
partition automatically and the `<LogViewer>` device/category filter works. At the syslog stage the user
enables UDM remote syslog (Settings → Control Plane → Integrations → Activity Logging/SIEM) → `192.168.2.148`,
all categories. Threat events appear in BOTH the structured alarm metric (alerting/dashboards) AND the syslog
stream (forensics) by design — only the alarm path pages.

## UI structure (deviation from spec §9.1 — see Amendment 2)

**Sidebar after the nav-restructure stage:**

```
Integrations ▸
  Home Assistant
  Docker
  Crons            (relocated from the removed Inventory page)
  Pi-hole          (EPIC-006)
  Synology         (EPIC-008)
  Unifi            (this epic — Unifi-as-product)
  Network          (this epic — network-as-concept; Clients tab → Client page)
```

**Three UI surfaces this epic builds:**

1. **Integrations → Unifi** (Unifi-as-product): header status strip (controller up/down · device up/total ·
   active-threat indicator); flat **device table** (8 devices: AP/switch/PDU/UDM state, model, firmware +
   update badge, cpu/mem/temp, uptime) → **device drill-down** (switch→port/PoE table; AP→radio/airtime/
   satisfaction table; PDU→outlet relays; UDM→system); **threats/IDS-IPS** list; **DPI** top-apps;
   **Teleport/VPN**; **controller/API health**; embedded `<LogViewer>` (UDM syslog). NO topology graph
   (deferred enhancement — table only).
2. **Integrations → Network** (network-as-concept, mostly Unifi-sourced): WAN/ISP status + speedtest history;
   **DNS-posture indicator** (clients handed `192.168.2.148` ✓ / drift); **DHCP pool usage** widget (uses
   authoritative lease count when the opt-in lease collector is on, active-client approximation when off);
   WiFi experience/airtime; SSID client distribution. **Clients tab** = the registry table (name/hostname,
   IP, MAC, AP+signal / switch+port, uptime, bandwidth, optional lease-expiry column; host first-class).
3. **Client page** (click any client row): identity + connection (AP+signal / switch+port / band) + uptime +
   bandwidth + per-client DPI + online/offline history + **DHCP lease** field (expiry / static-vs-dynamic;
   graceful "unavailable" when the lease collector is off). **EPIC-007 builds this richly; EPIC-006 enhances
   it** via a documented **DNS-enrichment extension point** (a stable contract, like the `<LogViewer>` embed
   contract, that EPIC-006 plugs DNS-behavior data into).

## Grafana (two dashboards, both embedded as Metrics tabs — EPIC-005 Metrics-tabs pattern)

- **`deploy/grafana/dashboards/unifi.json`** — gear-centric: device health, switch ports/PoE, AP airtime/
  satisfaction, threats/DPI, controller/API. Readability-review pass.
- **`deploy/grafana/dashboards/network.json`** — network-centric: WAN/ISP + speedtest, **DHCP pool/lease
  occupancy** (sharpened by the lease collector when on), client counts/distribution, WiFi experience.
  Readability-review pass.

## Scope-outs (deliberately NOT in this epic)

- **No write actions** (device restart / PoE-port cycle / PDU-outlet cycle) — read-only key, observe-only.
- **PDU outlet power/wattage** — Home Assistant already collects USP PDU Pro power; we report only outlet
  relay state/name, not wattage (explicit HA-overlap scope-out).
- **`unpoller` / SNMP** — dropped (direct-API covers metrics; SNMP dead on UDM).
- **Topology graph** — table only this epic; graph noted as a deferred enhancement.
- **Speedtest triggering, VPN config, per-SSID config inventory, historical `stat/report` pull** — out
  (VM is our history; read-only; YAGNI).
- **New-client → discovery/suggestion UX** — this epic emits the `UnifiNewClient` info alert; EPIC-011
  upgrades it into the unified suggestion inbox (note added to EPIC-011).
- **Scoping the SSH lease path** — the lease collector is built on EPIC-017's framework; the mandate to
  replace any currently-unscoped SSH access with a scoped forced-command user lives in EPIC-017.

## Stage decomposition (~25 stages, sequential within waves)

Each stage lands a single small slice and ships independently usable, mirroring EPIC-005's wave shape.
Wave order is sequenced for honest data dependencies (syslog before log-rules; all data collectors,
including the opt-in SSH lease, land in Wave B before any rules).

### Wave A — Foundation (4)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-001 | Unifi API client (key auth; classic + v1; `verify=false` self-signed; `api_took` capture) + `unifi_api_key` secret + lifespan wiring + smoke (`v1/sites`) |
| STAGE-007-002 | `integrations/unifi/` bundle skeleton + registration (mirror 005-003) |
| STAGE-007-003 | Persistent `unifi_clients` registry table (Alembic migration; MAC-keyed; time-stamped IP↔MAC observations; host first-class) |
| STAGE-007-004 | Cardinality caps (reuse 005-004) + identity-upsert helper (`stat/sta` + `stat/alluser` → registry) |

### Wave B — Collectors (8)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-005 | Combined device collector (health/firmware/ports/PoE/radios/PDU-relay/temp from one `stat/device`) | Complete |
| STAGE-007-006 | WAN/ISP + speedtest + failover collector (`stat/health` www) | Complete |
| STAGE-007-007 | Active-client identity collector → registry upsert | Complete |
| STAGE-007-008 | Per-client stats + WiFi-experience rollups collector | Complete |
| STAGE-007-009 | Per-client DPI collector (capped top-N×top-N + clamp) | Complete |
| STAGE-007-010 | Alarms/threats collector (`rest/alarm`) | Complete |
| STAGE-007-011 | DHCP config + DNS-steering + pool-usage collector (`rest/networkconf`) | Complete |
| STAGE-007-012 | SSH DHCP-lease collector (opt-in, default-OFF, on EPIC-017 framework; enriches registry with `lease_expiry`; surfaces in Client page / Clients-tab column / Network DHCP widget / `network.json` — graceful-degrade when off; note: built after EPIC-017) | Complete |

### Wave C — Health & VPN (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-013 | Controller-up composite + API-latency collector | Complete |
| STAGE-007-014 | VPN/Teleport health collector (confirm field shape live at Design) | Not Started |

### Wave D — Metric alert rules (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-015 | vmalert-METRICS rules (the ~24 metric alerts) — needs only Wave B/C collectors |

### Wave E — Events pipeline (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-016 | vector CEF `syslog` source + VL `service="udm-*"` labels + UDM-side remote-syslog enable + validate (BEFORE the log rules) |

### Wave F — Log alert rules (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-017 | vmalert-LOGS rules (the ~5 CEF-stream alerts) — after the syslog source exists |

### Wave G — UI (6)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-018 | **Nav restructure** (remove Inventory page; relocate Crons under Integrations; add Integrations entries) — FIRST UI stage so all subsequent UI registers into the final structure. Design verifies live what's on Inventory today. Cross-epic UI move (EPIC-002 crons provenance noted). |
| STAGE-007-019 | Backend panel/page data endpoints (Unifi panel + Network page + Client page) |
| STAGE-007-020 | Integrations → Unifi panel (device table + drill-down + threats/DPI/Teleport/controller + `<LogViewer>`) |
| STAGE-007-021 | Integrations → Network page shell + WAN/DHCP-pool/WiFi-experience/SSID widgets + DNS-posture indicator |
| STAGE-007-022 | Clients tab (registry table + lease-expiry column) + Client page (per-client drill-down + DPI + lease field + **DNS-enrichment extension point** for EPIC-006) |
| STAGE-007-023 | Embedded `<LogViewer>` wiring (UDM syslog, device/category filter) + threat-forensics view |

### Wave H — Grafana (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-007-024 | `unifi.json` (gear-centric) + Metrics-tab embed + readability pass |
| STAGE-007-025 | `network.json` (network-centric: WAN/speedtest/DHCP-occupancy/clients/WiFi) + Metrics-tab embed + readability pass |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:
- **Concurrency group `unifi`** for every collector hitting the UDM — never DDoS the controller.
- **Read-only key, observe-only** — the API key is used ONLY on GET; no write actions exist in this epic.
- **Unifi is the identity authority** — the persistent `unifi_clients` registry (MAC-keyed, time-stamped
  IP↔MAC observations) is the canonical client inventory EPIC-006 enhances; the host `192.168.2.148` is a
  first-class device.
- **No committed doc reveals the existing passwordless-root SSH trust to the router** — the SSH lease
  collector is opt-in/default-off and neutrally worded; scoping is EPIC-017's mandate.
- **Syslog before log-rules** — the CEF syslog source exists before the log-alert stage so rules validate
  against real lines.
- **Graceful degrade** — collectors degrade if optional fields absent (PDU power, Teleport block, lease
  collector off); cardinality caps prevent client/DPI explosion; emit `homelab_collector_run_*` self-metrics.
- **PSK fingerprints, full config dumps, and other ultra-sensitive UDM data** are never surfaced in our UI
  even if the API returns them.

## Dependencies

- EPIC-001 (kernel, secrets, registry).
- EPIC-003 (Docker) — the Synology/Unifi cross-reference; generic container surface (no back-fill here).
- EPIC-004 (`<LogViewer>` embed contract; vector→VL pipeline; per-`service` Drain models).
- EPIC-005 (integration-bundle skeleton/registration, cardinality cap, user-authored-rule machinery,
  panel/router/sidebar pattern, Metrics-tabs embed).
- **EPIC-017 (SSH probes) — built FIRST.** The opt-in DHCP-lease collector (STAGE-007-012) is built on its
  scoped forced-command framework.
- EPIC-016 — WAN-down from Unifi is the primary ISP-outage detector; EPIC-016 adds independent corroboration.
- **EPIC-006 (Pi-hole) — built AFTER this epic and ENHANCES its Client page / Clients-tab** with DNS
  behavior via the DNS-enrichment extension point (EPIC-006 STAGE-006-027, rewritten to "enhance, not
  rebuild").

## Notes

- Build sequence: EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008 (whole epics, sequential). Numbers unchanged.
- The pre-brainstorm 9-stage sketch is fully superseded by the ~25-stage decomposition above.
- The user leans toward MORE alerts / MORE detail — reflected in the ~24-alert catalog, per-client DPI, and
  the per-port/per-radio/per-outlet granularity.
- DPI per-client is cardinality-capped (top-N×top-N) with a clamp-rule for Ubiquiti's known per-counter
  spike bug. PDU outlet energy counters (also spike-prone) are NOT collected — HA owns PDU power.
- Candidate Claude auto-fix runbooks for EPIC-009 to author later: none high-value for an observe-only epic
  (network gear power-cycling is deliberately out of scope).
