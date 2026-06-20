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

## STAGE-007-013 — Controller-up composite + API-latency collector

- Live success path: run `UnifiControllerUpCollector.run()` against the live UDM with the valid read-only key -> `homelab_unifi_up == 1.0` (no labels), `homelab_unifi_up_reason{reason="ok"} == 1.0`, `homelab_unifi_api_took_seconds{endpoint="stat/sysinfo"}` present and > 0, result.ok True, metrics_emitted 3. (Validated live 2026-06-19: up=1.0, took=0.048s.)
- Auth-fail path: with a deliberately bad key the UDM returns HTTP 401 -> `homelab_unifi_up == 0.0`, `homelab_unifi_up_reason{reason="auth"} == 1.0`, NO `homelab_unifi_api_took_seconds` emitted, result.ok False, errors contains the HTTP 401 message, metrics_emitted 2. (Validated live 2026-06-19.)
- Unit suite: `test_unifi_controller_up_collector.py` covers null-ctx (`reason="not_configured"`), all 6 UnifiError reasons, bad_response (meta.rc!=ok / non-dict payload), empty_data (rc ok no records), and ok; 100% branch coverage of `controller_up.py`.
- Invariant: `homelab_unifi_up` MUST stay label-free (single stable series for the `UnifiControllerDown` critical alert keyed on `== 0`); failure reason lives on the separate `homelab_unifi_up_reason{reason}` info-gauge.

## STAGE-007-014 — VPN/Teleport health collector

- Live idle path: run `UnifiVpnTeleportCollector.run()` against the live UDM -> `homelab_unifi_teleport_up == 1.0` (no labels), `homelab_unifi_teleport_reason{reason="ok"} == 1.0`, `homelab_unifi_teleport_version{version="1"} == 1.0`, `homelab_unifi_api_took_seconds{endpoint="stat/device"}` present and > 0, result.ok True, metrics_emitted 4. (Validated live 2026-06-19: up=1.0, version="1", took~0.46s.)
- Reads `stat/device` (NOT `stat/health` — vpn.status is unconditionally "unknown" on this firmware and is intentionally NOT used). `teleport_up` derives from the gateway device record's non-empty `teleport_version`.
- Unit suite: `test_unifi_vpn_teleport_collector.py` covers null-ctx (`not_configured`), all 6 UnifiError reasons, bad_response (non-dict payload / meta.rc!=ok), device_not_found (non-list data / empty list), not_initialized (devices present, no teleport_version; incl. non-dict + int + empty-string teleport_version entries), and ok (version present). 100% branch coverage of `vpn_teleport.py`.
- Invariant: `homelab_unifi_teleport_up` MUST stay label-free (single stable series for the `UnifiTeleportDown` alert). HONEST MEANING: up=1 means "gateway reports Teleport initialized", NOT "a client session can be established".
- Deferred (session count): `homelab_unifi_teleport_sessions` is NOT emitted — no session-count field exists on this firmware. The dedicated endpoints `stat/remoteuserconnection` and `stat/teleport` return 404/error/empty even with Teleport configured (re-confirmed live 2026-06-19, idle). An active-session connect-from-phone probe is an OPTIONAL operator action (the phone is the user's physical device); if a future session field is ever discovered, it becomes a new tracked stage. The STAGE-007-015 `UnifiTeleportDown` alert keys on `teleport_up` (up/down), not a session count.

## STAGE-007-015 — vmalert-METRICS rules (unifi.yaml)
- `deploy/vmalert/metrics/unifi.yaml`: single group `unifi`, interval 30s, 25 alert rules. Validate syntax: `docker run --rm --entrypoint promtool -v $(pwd)/deploy/vmalert/metrics:/rules prom/prometheus:v2.47.0 check rules /rules/unifi.yaml` -> "SUCCESS: 25 rules found".
- Rule unit tests: `docker run --rm --entrypoint promtool -v $(pwd)/deploy/vmalert/metrics:/rules prom/prometheus:v2.47.0 test rules /rules/__tests__/unifi.tests.yaml` -> "SUCCESS" (covers the 23 non-subquery rules; rules 24/25 UnifiClientCountAnomaly + UnifiSsidClientCountAnomaly use `[1d:30s]` subqueries -> live-replay-only, no promtool case).
- promtool gotcha (v2.47.0): an OMITTED `exp_annotations` on a firing stanza asserts empty annotations (NOT skip) -> every firing test stanza carries the exact rendered summary+description. Rate/`changes()` rules with a `for:` need `eval_time >= rate_window + for` (e.g. eval_time 16m for rate([5m])+for:10m) or they show got:[] (pending, not firing).
- Live prod-rig validation (2026-06-19): after vmalert-metrics config reload (`POST /-/reload`), the `unifi` group loaded with all 25 rules `health=ok`, zero `lastError`; `UnifiControllerDown` inactive (controller up), anomaly rule inactive (subquery warm-up). Notifier wired to `http://alertmanager:9093`. `UnifiDnsSteeringMetricMissing` was genuinely FIRING — its `absent(homelab_unifi_dhcp_dns_primary)` matched because that metric was absent in prod VM at validation time (the absent() companion working as designed; whether the networkconf collector is emitting dhcp_dns_primary on prod is a separate OPERATOR question, not a rule defect).
- Re-validate after any edit to unifi.yaml: re-run promtool check + test, and reload vmalert-metrics; confirm the `unifi` group still reports 25 rules with health=ok and no lastError.
- Deferred items resolved: UnifiDhcpPoolExhaustion (#18) + UnifiDnsSteeringDrift (#5) built here; DPI spike clamp relocated to STAGE-007-024.

## STAGE-007-016 — UDM multi-format syslog parse

- **STAGE-007-016 — UDM multi-format syslog parse.** After any vector template change or UDM firmware update, re-verify the live parse: inject/observe each format and confirm correct bucketing.
  - Real UDM lines parse at ~0% failure: query `udm_lines_total{parse_failed="1"}` on vector:9598 (or VM) — should be ~0. A climbing parse_failed="1" counter means the UDM is emitting a format the `udm_parse` VRL doesn't handle (new firmware format, new event type). The `UnifiUdmLogParseFailed` vmalert rule (for: 0m) surfaces this immediately.
  - iptables firewall lines → `service=udm-firewall` with src/dst/fw_proto/fw_chain/fw_descr extracted (incl. quoted DESCR with spaces+brackets like "PortForward DNAT [Nginx SSL]"; empty `OUT=` must NOT swallow the next key).
  - systemd/sshd/daemon lines → `service=udm-system` with `process` set.
  - CEF audit events (login "Network Accessed" / config-change "Config Modified") → `service=udm-audit` with FULL space-containing values (`udm_admin`, `udm_settings_section`, `udm_settings_entry`) — validates the $$1 boundary pre-split. Both <PRI>-present and no-<PRI> CEF envelope shapes must parse.
  - Trailing-newline regression: real UDP datagrams carry a trailing `\n`; `strip_whitespace` must absorb it. The parametrized fixture test feeds each line with `+"\n"` to guard this.
  - Secrets: confirm tokens (e.g. mcad authkey) are redacted in VL — the `udm_authkey` redact pattern.
  - Vector reload: a vector template change needs `docker restart homelab-vector` AFTER the monitor re-renders (vector has no hot-reload — automated in STAGE-007-016A).
- **STAGE-007-016 — category vocabulary still being discovered.** The real `UNIFIcategory` values are bounded only by what's been observed (mostly "Audit" so far). New categories land in `udm-other` but are COUNTED (parse_failed stays 0 — they parse, just don't match a named bucket). Periodically review `service=udm-other` with parse_failed=0 to see if a new category deserves its own bucket.

## STAGE-007-017 — UDM log alert rules (deploy/vmalert/logs/unifi.yaml)

- **STAGE-007-017 — UDM log alert rules (deploy/vmalert/logs/unifi.yaml).** After any rule change or UDM firmware update, re-validate on the live rig:
  - Load gate: query the prod `homelab-vmalert-logs` `/api/v1/rules` — the `unifi_logs` group must show all 8 rules `health:"ok"` (no `lastError`). A rule going `health:"err"` means its LogsQL broke (e.g. a VictoriaLogs version change to `ipv4_range`/prefix-glob support).
  - Firing gate (4 confirmed-real, validated live this stage): plant VL records → confirm fire to Alertmanager with `integration:unifi` labels. UnifiAdminLoginLog (cef_signature_id 544), UnifiConfigChangeLog (546), UnifiWanBlockSpikeLog (`fw_chain:WAN_LOCAL-D*`, >20/5m, `for:5m` so it goes PENDING first — pending is correct), UnifiOomPressureLog (`process:="earlyoom" "sending SIGTERM"`).
  - **Integration test:** `apps/monitor/tests/integration/test_vmalert_unifi_logs_pipeline.py` (run via `make integration`) is the CI regression artifact (plants lines via the logs-test twin's lowered thresholds + asserts fire).
  - **RESOLVED this stage (live-validated):** (a) `ipv4_range` LogsQL syntax is VALID + correctly excludes LAN src (UnifiAdminLoginExternalLog works) — no glob-negation fallback needed. (b) The earlyoom `"sending SIGTERM"` substring correctly discriminates real kills from the periodic `mem avail:` heartbeat (heartbeat records did NOT fire the rule).
  - **STILL UNVALIDATED (refine when a real event is observed — marked `# UNVALIDATED` inline in the rule file):** UnifiPortFlapLog, UnifiDeviceDisconnectLog, UnifiFirmwareEventLog substring patterns — no such events appeared in the STAGE-016 capture; their substrings are best-guesses against the `udm-system` channel. When a real port-flap / device-disconnect / firmware-update event is observed in VL, confirm the substring matches and refine if needed. Their LogsQL is health:ok (valid syntax); only the substring CONTENT is unvalidated.
- **STAGE-007-017 — keep the logs-test twin in sync.** `deploy/vmalert/logs-test/unifi.yaml` must stay in sync with the prod `deploy/vmalert/logs/unifi.yaml` for any label/annotation/alert-name change (only `_time`/`interval`/`filter` thresholds may diverge). The integration test depends on the twin's lowered thresholds.
