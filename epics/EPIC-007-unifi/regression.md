# Regression Checklist - EPIC-007: Unifi

(Items added per stage during Refinement.)

## STAGE-007-001 (Unifi API client)

- **STAGE-007-001 (Unifi API client):** With the real `unifi_api_key` (read-only) set and the live UDM reachable, the client must reach BOTH API surfaces over self-signed TLS (`verify=False`): `v1_sites()` → 200 with a site UUID; `resolve_site_id()` caches the UUID into `v1_site_id` while `site_name` stays `"default"`; `stat_sysinfo()` (classic, uses `site_name`) → 200; `v1_devices()` (v1 site-scoped, uses `v1_site_id`) → 200. The API key must NEVER appear in logs or error messages. Regression guard: classic URLs must use the short site NAME, not the v1 UUID (a UUID in the classic path 401s).

- **STAGE-007-002 (Unifi bundle skeleton):** On backend startup the `unifi` integration bundle must register without a `unifi_integration.collector_register_failed` warning, and `GET /api/collectors` must list a `unifi_placeholder` collector (status healthy, interval 60s, run_kind async). The extended `UnifiConfig` (`host_lan_ip` default 192.168.2.148 / env `HOMELAB_MONITOR_UNIFI_HOST_LAN_IP`; `ssh_lease_enabled` default False / env `HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED`) must validate at startup. NOTE: the `unifi_placeholder` collector is SCAFFOLDING — STAGE-007-005 removes it; after that, this regression item's placeholder check no longer applies (the bundle should instead list the first real Wave-B collector).

- **STAGE-007-003 (unifi_clients registry):** Migration `0044` must apply (up) and reverse (down) cleanly, creating/dropping `unifi_clients` + `unifi_client_observations`. Against a migrated DB, `UnifiClientRepo` must: seed exactly one idempotent host row (`ensure_host_row` → sentinel `host:<ip>`, `is_host=1`); upsert-by-MAC preserving `first_seen` while updating mutable fields + `last_seen`; collapse repeated (mac,ip) observations into one span and inline-prune spans older than the configured retention (`observation_retention_days`, default 90, env `HOMELAB_MONITOR_UNIFI_OBSERVATION_RETENTION_DAYS`); and resolve `find_mac_by_ip_at(ip, at)` to the mac whose span covers `at` (None when no span matches). NOTE: the sentinel `host:<ip>` row's reconciliation into the real-MAC row is STAGE-007-004/007 (collector-side); the `lease_expiry` column ships nullable here and is written by STAGE-007-012 (no migration).

- **STAGE-007-004 (identity-upsert helper + caps):** `upsert_identity(conn, stat_sta, stat_alluser, host_lan_ip, observation_cutoff, now)` must: two-pass merge (sta=online upsert+observation; alluser=offline-only upsert for unseen macs, no observation, no downgrade of already-online macs); convert epoch-int `first_seen`/`last_seen` → ISO (seeding registry `first_seen` from the RECORD, not now); use `last_ip` for offline clients' ip; reconcile the sentinel `host:<ip>` row into the real-mac row (is_host=1, first_seen merged, sentinel deleted) when a record's ip == host_lan_ip; skip + count malformed (no/non-str mac) records; return accurate `UpsertResult{clients_upserted, observations_appended, hosts_reconciled, skipped}`. `upsert_client_conn` now takes a `first_seen` param (INSERT-only, preserved on conflict). Cardinality-cap families `unifi_client_stats`/`unifi_dpi` are CONFIG-only here — APPLICATION is STAGE-007-008/009. The LIVE-UDM exercise is STAGE-007-007.

## STAGE-007-005 — Combined Unifi device collector

- [ ] Run the `unifi_device` collector against the live UDM (or replay a captured `stat/device` payload) and confirm: `ok=True`, all adopted devices emit `homelab_unifi_device_up`, and the families `homelab_unifi_port_*`, `homelab_unifi_radio_*`, `homelab_unifi_outlet_relay_state`, `homelab_unifi_device_temperature_celsius`, `homelab_unifi_api_took_seconds{endpoint="stat/device"}` are present.
- [ ] Confirm NO `homelab_unifi_outlet_power/current/voltage` metric is emitted (PDU wattage is intentionally omitted; HA owns it).
- [ ] Confirm the collector does not raise on malformed/absent fields (graceful degrade): a device with empty `sys_stats`/missing `system-stats`/non-list tables emits no spurious metrics and does not fail the run.
- [ ] Confirm the `unifi_placeholder` collector is GONE from `GET /api/collectors` and `unifi_device` is present (healthy).

## STAGE-007-006 — WAN/ISP + speedtest + failover collector

- [ ] Run the `unifi_wan` collector against the live UDM (or replay a captured `stat/health` payload) and confirm `ok=True` and these families present: `homelab_unifi_wan_up`, `_wan_latency_seconds` (value in SECONDS, not ms), `_wan_drops`, `_wan_xput_down/up_bytes_per_sec`, `_wan_failover_capable`, `_wan_failover_active`, `homelab_unifi_speedtest_lastrun`, `homelab_unifi_api_took_seconds{endpoint="stat/health"}`.
- [ ] Confirm the never-run-speedtest graceful degrade: when `speedtest_lastrun==0`, the `homelab_unifi_speedtest_download_mbps`/`_upload_mbps`/`_ping_seconds` metrics are NOT emitted (zeros are not reported as real speedtest results), while `_speedtest_lastrun` IS emitted (=0).
- [ ] Confirm failover semantics: `_wan_failover_capable=1.0` when a secondary WAN is configured (even if down); `_wan_failover_active=1.0` only when a secondary WAN is actively carrying traffic (single-active-WAN -> 0.0).
- [ ] Confirm the collector does not raise on malformed/absent subsystem entries (graceful degrade): missing `www` or `wan` subsystem entry, non-dict `data` entry, non-string `status`, non-dict `uptime_stats` -> ok=True, no spurious metrics.
- [ ] Confirm both `unifi_device` and `unifi_wan` collectors appear healthy in `GET /api/collectors`.

## STAGE-007-007 — Active-client identity collector
- [ ] Run the `unifi_active_client` collector against the live UDM (or replay captured stat/sta+stat/alluser payloads) into a migrated DB with the host sentinel seeded; confirm `ok=True`, the registry upserts the roster (online + offline rows), and `homelab_unifi_identity_hosts_reconciled`=1 (host `192.168.2.148` reconciled — requires the `host:<ip>` sentinel seeded by lifespan/ensure_host_row first).
- [ ] Confirm new-client detection: on a registry that already contains the macs, a re-run emits `homelab_unifi_new_client_total`=0 (no false new-client signals); a genuinely new mac increments it. (Detection is a pre-upsert MAC-set snapshot diff, NOT `first_seen==now`.)
- [ ] Confirm roster rollups present: `homelab_unifi_active_client_count`, `_known_client_count`, `_offline_client_count`, `ssid_client_count{ssid}`, `client_count_by_network/by_ap/by_band/by_link` (bands 2.4ghz/5ghz/6ghz; link wired/wireless).
- [ ] Confirm D2 degrade: `stat_alluser` failure -> ok=True + `homelab_unifi_alluser_degraded`=1 + sta still upserted; `stat_sta` failure -> ok=False + no upsert.
- [ ] Confirm both `api_took_seconds{endpoint=stat/sta}` and `{endpoint=stat/alluser}` emitted on success.
- [ ] Confirm `unifi_device`, `unifi_wan`, `unifi_active_client` all appear healthy in `GET /api/collectors`.

## STAGE-007-008 — Per-client stats + WiFi-experience rollups collector
- [ ] Run the `unifi_client_stats` collector against the live UDM (or replay a captured `stat/sta` payload); confirm `ok=True` and the 6 capped per-client families emit `{mac}`-labeled series: `homelab_unifi_client_signal_dbm` (wireless, negative dBm), `_client_tx_rate_bps`/`_client_rx_rate_bps` (wireless, BPS magnitude — confirm kbps->bps x1000, NOT raw kbps), `_client_uptime`, `_client_tx_bytes`, `_client_rx_bytes` (all clients; wired bytes from wired-* keys).
- [ ] Confirm the cardinality cap: each capped family writes a `homelab_metric_family_dropped_series{family}` gauge (0.0 under cap); when >`cap_for("unifi_client_stats")` (200) clients are fed, survivors==cap and exactly one warning SuggestionEvent per over-cap family. The survivor `{mac}` set is IDENTICAL across families (A2 invariant).
- [ ] Confirm WiFi-experience rollups (bounded, not capped) with `{threshold}` labels: `clients_poor_signal{threshold="-70"}`, `clients_poor_satisfaction{threshold="50"}`, `clients_high_retries{threshold="10"}` (computed over the full pre-cap list; div-by-zero guarded when `wifi_tx_attempts==0`), and `ap_client_count{ap_mac}` (per-AP wireless client counts).
- [ ] Confirm wired graceful degrade: a wired client contributes uptime + tx/rx_bytes but NO signal_dbm/tx_rate_bps/rx_rate_bps series.
- [ ] Confirm all four unifi collectors (device, wan, active_client, client_stats) healthy in `GET /api/collectors`.

## STAGE-007-009 — Per-client DPI collector (capped top-N×top-N + clamp)
- [ ] Run `UnifiClientDpiCollector` against the live UDM and confirm `homelab_unifi_dpi_enabled=1.0` + `homelab_unifi_api_took_seconds{endpoint="stat/stadpi"}` + `homelab_metric_family_dropped_series{family="homelab_unifi_client_dpi_bytes"}` are emitted, with `result.ok=True`, even when DPI data is empty (graceful-degrade path).
- [ ] When the live UDM accumulates DPI data, re-validate: `homelab_unifi_client_dpi_bytes{client,app,cat}` series appear with combined rx+tx values, cardinality bounded to `cap_for("unifi_dpi")` (=100) by VOLUME (biggest consumers survive), and the drop gauge + ONE SuggestionEvent fire when over cap. (This populated-live path was NOT exercisable at STAGE-007-009 Refinement — live had no DPI data.)
- [ ] Confirm the by-volume cap keeps the HIGHEST-byte series (not lexically-smallest) — the deliberate deviation from CappedEmitter's lexical slice.

## STAGE-007-010 — Alarms/threats collector

- [ ] Run the `unifi_alarms` collector against the live UDM and confirm `homelab_unifi_threat_count{}` is ALWAYS emitted (=0.0 on a quiet network) plus `homelab_unifi_api_took_seconds{endpoint="rest/alarm?archived=false"}`, with `result.ok=True` and no per-type series when no alarms (the always-emit-0 / graceful path).
- [ ] When the live UDM has active alarms, re-validate: `homelab_unifi_threat_count{}` = distinct active `_id` count and `homelab_unifi_threat{type}` per-type counts appear, with the `{type}` label = alarm `key` (fallback `subsystem`/`"unknown"`). (This populated-live path was NOT exercisable at STAGE-007-010 Refinement — live had zero alarms.)
- [ ] Confirm duplicate alarm `_id`s are counted ONCE (within-poll dedup) and records with no usable `_id` are skipped.

## STAGE-007-011 — DHCP config + DNS-steering + pool-usage collector

- [ ] Run `UnifiNetworkconfCollector` against the live UDM and confirm: `homelab_unifi_dhcp_enabled_network_count>=1`, `homelab_unifi_dhcp_pool_size{network="Default"}` ~249, and `homelab_unifi_dhcp_dns_primary{network="Default", dns="192.168.2.148"}=1.0` (the Pi-hole DNS-steering signal — if the `dns` label ever != 192.168.2.148, DNS steering has drifted). WAN networks must NOT produce DHCP series.
- [ ] Confirm field-by-field graceful degrade: a DHCP network with a malformed pool start/stop drops only its pool gauges (size/start/end) while still emitting its dns_primary gauge and still counting toward `dhcp_enabled_network_count`; a network with no `dhcpd_dns_1` emits pool gauges but no dns_primary gauge.
- [ ] Confirm `homelab_unifi_dhcp_enabled_network_count` is ALWAYS emitted (incl. 0.0 if no DHCP networks) so STAGE-007-015 alerts avoid the absent() trap.

## STAGE-007-012 — SSH DHCP-lease collector (opt-in, default-OFF, on EPIC-017 framework)

- [ ] With the gate OFF (default), confirm `UnifiSshLeaseCollector` is inert: ok=True, 0 metrics, no SSH attempt, no DB write. (The disabled default must never touch SSH or the registry.)
- [ ] With the gate ON + a synthetic/fixture lease file, confirm: `homelab_unifi_dhcp_lease_count` = count of valid leases; existing registry rows get `lease_expiry` set by CASE-INSENSITIVE MAC match (registry MACs are stored verbatim from the API; dnsmasq lowercases); lease-only MACs get new rows (online=False); and the collector emits the `_probe_`-infixed health metrics (`homelab_ssh_probe_up`/`_host_key_mismatch`/`_duration_seconds`/`homelab_ssh_last_success_age_seconds` with `probe="unifi_dhcp_lease"`) — and NEVER the bare `homelab_ssh_up{target}` (no collision with the SSH-bundle uptime probe).
- [ ] PENDING/operator: once the operator configures the `udm` ssh_target + installs the forced-command key + sets `HOMELAB_MONITOR_UNIFI_SSH_LEASE_ENABLED=1`, run the collector against the live UDM (READ-ONLY) and confirm a real lease read enriches the registry with `lease_expiry` + emits `homelab_unifi_dhcp_lease_count`. (This live path was WAIVED at STAGE-007-012 Refinement — no key installed.)
- [ ] On a transport failure (host-key mismatch, auth, timeout) or non-zero exit, confirm the collector returns ok=False and does NOT emit `homelab_unifi_dhcp_lease_count` (so STAGE-007-011 occupancy never misreads a probe failure as zero leases) and does NOT write the registry.
