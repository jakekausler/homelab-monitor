# EPIC-008: Synology integration

## Status: In Progress (current: STAGE-008-032 Complete [prerequisite, unblocked 018/019]; next: resume STAGE-008-019 Build)

## Build order + framework dependencies (LOCKED — 2026-06-22 brainstorm)

**Whole-epic build sequence: EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008** (epic numbers unchanged; only
build order is sequenced). EPIC-008 is built LAST of the four. EPIC-017 (SSH probe framework), EPIC-007
(Unifi), and EPIC-006 (Pi-hole) are all COMPLETE, so every dependency this epic relies on is satisfied:

- **EPIC-017 (SSH probe framework) is DONE** — the Synology SSH probe (STAGE-008-014) is built on its scoped
  per-target `dedicated-user` + forced-command framework from day one (no "unscoped now, harden later"
  debt). EPIC-017 ships the mechanism + the `uptime` exemplar; the Synology-specific probe body is THIS
  epic's deliverable.
- **EPIC-005 (Home Assistant) is the structural exemplar** for the integration bundle (client + secret +
  lifespan wiring, `integrations/<name>/` bundle skeleton, cardinality cap, panel/router/sidebar
  registration, embedded `<LogViewer>`, Grafana Metrics-tab embed). EPIC-006/007 are the most recent
  precedents and refined the client-session + syslog-pipeline + two-page patterns this epic copies.
- **EPIC-007 (Unifi) is the closest parallel** — same shape: a real API client over a self-signed TLS
  endpoint (`verify=False`), direct-API collectors (no exporter sidecar), a syslog → vector → VL events
  pipeline, two Grafana dashboards + two Integrations pages, observe-only. This epic mirrors EPIC-007
  heavily.

## Brainstormed architecture (2026-06-22, recon-grounded — supersedes the pre-brainstorm placeholder)

The pre-brainstorm placeholder assumed the spec's §2 Q12 "three paths" triad — (A) SNMP via `snmp_exporter`
+ Synology MIB, (B) DSM API, (C) DSM syslog forwarding. **Live read-only recon (2026-06-22) against the
real DS3622xs+ reshaped this** (mirroring how EPIC-007's recon dropped `unpoller`). The decisions below are
LOCKED; stage Design phases inherit them and do not re-litigate.

### Amendment 1 — DROP SNMP entirely (`snmp_exporter` + Synology MIB)

SNMP is **redundant** here and adds a sidecar + a user-side enable step for a strict subset of what the DSM
API already provides. Recon: SNMP is OFF on the NAS (off by default; needs a DSM enable + community/v3
setup) AND no net-snmp tooling exists on the monitor host. Meanwhile the **DSM API exposes the entire
hardware/storage surface** SNMP would (volumes, pools, disks, SMART status, RAID/SHR, temps, fans, NIC,
UPS) **plus** the API-only signals. So: **no `snmp_exporter`, no MIB, no SNMP collector.** ("Drop the extra
thing that adds nothing" — the user's explicit guidance.)

### Amendment 2 — DSM API is PRIMARY (admin account); SSH is the independent cross-check + unique-data path

- **DSM API** (HTTPS `:5001`, self-signed `CN=synology` → `verify=False`; `SYNO.API.Auth` version 7; ALL
  routes via `/webapi/entry.cgi`; one reused session, re-auth on error 119; **`synology` concurrency
  group**; observe-only). This is the workhorse for storage/disks/SMART-status/RAID/scrub, system/temps,
  utilization, UPS, updates/packages, Hyper Backup, snapshot/replication, Surveillance Station, Security
  Advisor status, active connections.
- **The service account `homelab-monitor` is a DSM ADMINISTRATOR** (recon proved a non-admin account hits
  error 105 on nearly every system-health API — storage/SMART/utilization/backup/security/upgrade — and
  Synology has no "read-only admin" role). The user accepted admin membership to capture the full surface;
  **our collector code only ever calls read methods and the epic is observe-only**, so the practical
  exposure on this single-user homelab is low (same model EPIC-006 noted for Pi-hole's RO app password not
  being a hard server-side boundary). Documented deviation from "read-only DSM credentials" → recorded
  here.
- **SSH is NOT redundant**: it is (a) the **independent liveness anchor** (a different failure domain —
  survives a DSM-web-stack hang; the EPIC-014/016 "the liveness check must not route through the thing
  being checked" discipline), (b) the **only source of full per-attribute SMART** (the WebAPI does NOT
  expose the SMART attribute table — recon: `Storage.CGI.Smart` → 121/103; `synodisk --smart_info_get`
  gives the raw ~21 attributes/disk unprivileged), (c) raw `/proc/mdstat` rebuild/resync progress, and (d)
  raw `upsc` NUT variables the UPS API omits (input voltage, load %, battery date). EPIC-017 also MANDATES
  this epic render `homelab_ssh_*` probe state on the panel + dashboard.

### Amendment 3 — NO sudoers; unprivileged SSH only

The 008 placeholder + EPIC-017 banner assumed Synology SMART/btrfs need root via a narrow NOPASSWD sudoers
wrapper. Recon **disproves the SMART half**: `synodisk --enum` / `--smart_info_get` give full per-disk SMART
+ temps with ZERO privilege. Only **btrfs scrub status** genuinely needs root — and the **DSM API**
(`storagePools[].scrubbingStatus` + `progress.percent` in `Storage.CGI.Storage load_info`) provides it
without SSH-root. So: **no NOPASSWD sudoers entry.** The dedicated low-priv SSH user reads everything
unprivileged. (Honors the EPIC-017 mandate; just doesn't grant sudo.)

### Amendment 4 — Separate low-priv SSH user `homelab-probe` (distinct from the admin API account)

The DSM API uses the admin `homelab-monitor` account. The SSH probe uses a **NEW dedicated low-privilege
DSM user `homelab-probe`** (EPIC-017 `dedicated-user` mode, forced command, no sudo) — NOT the admin
account, and NOT the existing privileged human-ops key. This honors the EPIC-017 low-priv defense-in-depth
mandate: a leaked forced-command key lands on a low-priv shell, not an admin one. Two least-privilege
identities, one per transport.

### Amendment 5 — Logs: syslog forwarding ONLY (no API log polling)

**No DSM API is used for log polling** (user's explicit rule). All event/log signals arrive via **DSM
remote-syslog forwarding → vector → VictoriaLogs**, mirroring EPIC-007's UDM syslog pipeline (the *method*
mirrors Unifi; DSM emits standard RFC syslog, NOT Unifi's CEF — the parser differs; verify the live format
at STAGE-008-020 Design). We forward **all logs DSM can emit**, with `service="synology-*"` labels (per
facility), Synology-specific redaction patterns, and an embedded `<LogViewer>`. A **syslog-vs-metric
coverage map** is recorded at the syslog stage so any category DSM does NOT forward is explicitly noted as
covered by the metric collectors instead (no silent gaps). **Single source per signal:** if a signal
arrives via syslog→VL, derive it from VL (vmalert-logs) — do NOT also API-poll it. Only API-source what
does NOT arrive via syslog (e.g. Security Advisor scan *status* is a periodic scan result, not a log event
→ API metric).

### Amendment 6 — Observe-only (no write actions to the NAS)

The Synology is the most critical infra on this LAN (backups, surveillance, NFS to containers); write
actions are high-blast-radius. Even though the API account is admin (so the server-side boundary is weak),
**our code never calls a DSM write method.** No write endpoints, no service/container lifecycle controls, no
backup-trigger / SMART-test / alert-ack actions. (Mirrors EPIC-007's observe-only posture.)

### Amendment 7 — Two Grafana dashboards AND two Integrations pages

- **`synology.json`** + **Integrations → Synology** page: storage/disks/SMART/RAID/scrub, system/CPU/mem/
  temps, UPS, backup/replication, updates/packages, Security Advisor + connections, mount health, SSH-probe
  state.
- **`synology-surveillance.json`** + **Integrations → Surveillance** page: cameras (online/recording),
  per-camera events/motion, recordings, license, HomeMode.
- The **Surveillance** entry is placed **alphabetically** in the Integrations sidebar (…Pi-hole,
  Surveillance, Synology, Unifi…). Each page embeds its dashboard as a Metrics tab (the EPIC-005 Metrics-tab
  pattern).

### Amendment 8 — Mount health + Docker suppression built BEFORE the rules/UI/dashboards

The NFS/`/rackstation` mount-health collector (a data source) and the Alertmanager inhibit-rules
(`mount_up==0` inhibits dependent `ContainerDown` alerts — the epic's "mount-health before container probes"
criterion) are built in **Wave F**, BEFORE the alert rules (Wave H), UI (Wave I), and Grafana (Wave J) — so
mount data + suppression exist before anything references them. The full thing is built in this epic.
**Instance-B note:** Docker monitoring + host-integration are DISABLED in instance B (this checkout), so the
mount-health probe (reads the host mount table) and the Docker-suppression (needs the EPIC-003 alert path)
are **instance-A-validation stages**: build here → push to remote → pull into instance A
(`/storage/programs/homelab-monitor`) → validate/finish there (the user pauses instance-A work during those
stages). Marked per-stage.

## Overview

Land Synology DS3622xs+ as a first-class integration bundle, mirroring EPIC-005/006/007. Full treatment: a
real DSM v7 REST client; a comprehensive suite of direct-API collectors (storage/disks/SMART-status/RAID/
scrub, system/temps, utilization, UPS, Hyper Backup, snapshot/replication, updates/packages, Security
Advisor + connections, Surveillance Station cameras/events/recordings/license/HomeMode); a combined
unprivileged SSH probe (liveness + full per-attribute SMART + raw `/proc/mdstat` + raw `upsc`) on the
EPIC-017 framework; an NFS/`/rackstation` mount-health probe + Alertmanager Docker-alert suppression; a DSM
syslog → vector → VL events pipeline; a default alert catalog (metrics + logs); **two** Grafana dashboards;
and **two** operator-facing Integrations pages (Synology + Surveillance). **Observe-only.**

The Synology is the most critical infra in this homelab — it holds backups, media, surveillance recordings,
and serves NFS/SMB to several Docker containers — so its failure is high-impact.

This epic **consumes** foundation already built by EPIC-001/003/004/005/017 and does NOT rebuild it: the
integration-bundle skeleton + registration pattern (005-003), the reusable cardinality cap (005-004), the
`<LogViewer>` embedding contract (004-003), the vector→VL pipeline + per-`service` Drain models (EPIC-004),
Grafana-dashboards-as-code, the vmalert metrics+logs surfaces, and the SSH probe framework (EPIC-017).

## Source documents (read before starting any stage)

- Master design spec §2 Q12 (Synology decisions — AMENDED above), §3.4 (discovered targets), §5
  (plugin/collector/integration_bundle framework + concurrency model), §6.2 (`homelab_synology_*` metric
  family naming), §9.2 (Integrations panel = plugin-provided panel; Metrics = Grafana embed).
- EPIC-005 (`epics/EPIC-005-home-assistant/`) — the exemplar integration bundle.
- EPIC-007 (`epics/EPIC-007-unifi/`) — the closest parallel (self-signed API client, direct-API collectors,
  syslog pipeline, two dashboards/pages, observe-only).
- EPIC-006 (`epics/EPIC-006-pihole/`) — the most recent client-session + integration-README precedent.
- EPIC-017 (`epics/EPIC-017-ssh-probes/`) — the SSH probe framework this epic's probe is built on.
- `apps/ui/src/components/logs/README.md` — the `<LogViewer>` embedding contract.
- Project memory `reference_homelab_inventory.md` / `reference_docker_inventory.md` — Synology details.

## Verified deployment reality (read-only recon 2026-06-22 — re-verify live in each stage's Design)

- **DS3622xs+**, DSM **7.3.2-86009**, model `synology_broadwellnk_3622xs+`, serial `24C0SQR660B5C`, IP
  `192.168.2.4`, hostname `NAS`, CPU Xeon D-1531 (6 cores), 16 GB RAM, uptime ~49d.
- **DSM API:** HTTPS `:5001` (and HTTP `:5000`), self-signed `CN=synology` (use `verify=False`). 778 APIs
  in the `SYNO.API.Info` catalog, ALL via `/webapi/entry.cgi`. `SYNO.API.Auth` **version 7** — login with
  `account`/`passwd`/`format=sid`, **NO `session=` param** (sending one returns error 402 — this caused a
  false "2FA" diagnosis during recon; it is NOT 2FA). Login returns `sid` + `synotoken`; **error 119** =
  session expired (re-auth); **400** = bad creds. 2FA NOT required for this account.
- **Storage** (`SYNO.Storage.CGI.Storage` method `load_info`, ONE call = volumes+pools+disks+caches):
  1 volume `volume_1` (btrfs, RAID6, `/volume1`, ~**80% full**, status `has_unverified_disk`,
  `space_status=fs_almost_full`); 8 disks (mixed 10/12/22 TB; per-disk `model`/`serial`/`temp`/
  `smart_status=normal`/`unc`/`remain_life`/`sb_days_left*`; sdc hottest at 50 °C); 1 pool `reuse_1`
  (`scrubbingStatus=ready`, `progress.percent=-1` idle, RAID `raidStatus=1` 8/8 disks). **Detailed
  per-attribute SMART is NOT in the WebAPI** (`Storage.CGI.Smart` → 121/103) → SSH `synodisk` only. **Scrub
  status lives in the pool object**, not a separate API.
- **System** (`SYNO.Core.System` v3 method `info`): model/serial/firmware, `sys_temp=50`, uptime,
  CPU/RAM specs, USB-attached UPS. **`SystemHealth` `rule`** = active overall-health condition (currently
  `storage_is_attention`). **`Hardware.FanSpeed`** = status strings only (no numeric RPM). `NeedReboot` =
  bool.
- **Utilization** (`SYNO.Core.System.Utilization` method `get`): CPU load, mem `real_usage`, swap %, per-
  disk IO, per-NIC rx/tx, NFS OPS+latency.
- **UPS** (`SYNO.Core.ExternalDevice.UPS` method `get`): APC Smart-UPS 1500 over USB — `charge=100`,
  `runtime` (s), `status=usb_ups_status_online` enum, model/manufacture. No load%/voltage via API (→ SSH
  `upsc`).
- **Updates:** `SYNO.Core.Upgrade.Server` v4 method `check` → DSM update **available** ("Update 3",
  `isSecurityVersion:true`). `SYNO.Core.Package` (17 installed, running/stopped status) +
  `SYNO.Core.Package.Server` (available updates catalog).
- **Hyper Backup:** HyperBackup 4.2.1 installed + running, **0 configured jobs** (`Backup.Task list` →
  empty). **Snapshot Replication / DR: none configured** (`Share.Snapshot` empty; `Btrfs.Replica.Core` no
  working read method found via guessed names — nail down at STAGE-008-011 Design). Collectors must handle
  the empty case gracefully (a `SynologyNoBackupConfigured` signal is itself useful).
- **Surveillance Station** installed (SS 9.2.4): 3 cameras (id1 Driveway `192.168.2.103`, id2 Backyard
  `.99`, id3 Doorbell `.215`; all added as generic ONVIF — vendor field says ONVIF, not "Reolink"),
  `Camera.List.status=1` = ok, `Camera.Status` v3 read method NOT found (derive online/offline from
  `Camera.List.status` + `SS.Log` connection-lost/restored), `Event.CountByCategory` (per-camera + per-day
  event counts), `Recording.List` (2645 clips + sizes), `License.Load` (3/3 used, max 90), `HomeMode.GetInfo`
  (`on` = armed/disarmed).
- **Security / connections:** `SecurityScan.Status` method **`system_get`** (Security Advisor:
  `sysStatus=risk` currently, per-category fail counts); `SecurityAdvisor.LoginActivity` (geo-anomalous
  logins — city/country/IP/user; *prefer deriving login events from syslog→VL per Amendment 5*);
  `CurrentConnection` (active sessions — who/from-IP/service); `User` (accounts).
- **Syslog forwarding status NOT readable** via `SyslogClient.Status` (method not found — likely a
  `LogCenter` API). User enables DSM remote-syslog AT the syslog stage; we do NOT API-poll logs.
- **SSH** (port `53197`): current access lands as privileged human-ops `jakekausler` (uid 1026,
  administrators+root) — the unscoped path EPIC-017 replaces. **No passwordless sudo.** Everything we need
  reads UNPRIVILEGED: `/usr/syno/bin/synodisk --enum` (per-disk model/capacity/**temp**) +
  `--smart_info_get /dev/sdX` (full ~21-attribute SMART), `/proc/mdstat` (RAID), `/usr/bin/upsc ups` (full
  UPS NUT vars), hwmon sysfs (CPU temp; `sensors` not installed), `df`/`mount` (volume 80%, btrfs),
  `/etc/VERSION`. SMART/btrfs via `smartctl`/`btrfs` need root (NOT used). No NVMe.

## Credential / transport model (LOCKED)

- **`synology_dsm_password`** in the encrypted secrets store — the admin `homelab-monitor` account's
  password (single credential; observe-only; no RO/RW split). Read per-request from the TTL-cached resolver,
  never stored on the client, never logged.
- **`ssh_probe_key_synology`** in the secrets store — the ed25519 key for the `homelab-probe` SSH user
  (generated by the EPIC-017 `hm ssh-probe keygen`). Distinct from the admin API credential and from the
  existing human-ops keys.
- **One `synology` concurrency group** for every DSM-API collector (never hammer the NAS). The SSH probe
  uses its own per-target concurrency group per the EPIC-017 framework.
- **Self-signed TLS** — DSM client uses `verify=False` (pin `CN=synology`), like EPIC-007's Unifi client.
- **Observe-only** — the API account is admin but our code calls ONLY read methods; no write paths exist.

## Metric families (all `homelab_synology_*`, cardinality-capped, single `synology` group)

`homelab_synology_api_took_seconds{api}` emitted from every DSM response (free latency signal). SSH probe
metrics use the EPIC-017 `homelab_ssh_*` family + `homelab_synology_smart_attr{disk,attribute}` etc. Naturally
bounded series (8 disks, 1 volume/pool, 3 cameras, ~17 packages) emitted in full; SS events/recordings kept
as AGGREGATE counters only; the inherited cardinality cap is a guardrail.

| Collector | Source | Cadence | Emits (abridged) |
|---|---|---|---|
| Storage volumes+disks | `Storage.CGI.Storage load_info` | 5m | per-volume usage/status/fs; per-disk model/temp/`smart_status`/`unc`/`remain_life`/`sb_days_left` |
| Pool & RAID | `Storage.CGI.Storage load_info` (pool obj) | 5m | `raidStatus`, normal/designed disk count, rebuild `progress.percent`, `scrubbingStatus`, unverified-disk attention |
| System info + temps | `Core.System` v3 `info` | 60s | model/serial/uptime/`sys_temp`/fan status; `NeedReboot` |
| Utilization | `System.Utilization get` | 60s | cpu load, mem/swap %, per-disk IO, per-NIC rx/tx, NFS OPS+latency |
| UPS + health rule | `ExternalDevice.UPS get` + `SystemHealth get` | 60s | charge%, runtime, status enum, model; SystemHealth `rule` id as status metric |
| Hyper Backup | `Backup.Task`/`Repository list` | 5m | per-job last-run/result/size/next-run; `SynologyNoBackupConfigured` when 0 tasks (graceful-empty) |
| Snapshot/replication | `Share.Snapshot`/`Btrfs.Replica.Core list` | 5m | per-share snapshot age/count, replication lag (graceful-empty) |
| Updates & packages | `Upgrade.Server check`, `Package`/`Package.Server list` | 1h | DSM update available (+`isSecurityVersion`), per-package running/stopped + update-available |
| Security + connections | `SecurityScan.Status system_get`, `CurrentConnection list` | 1h / 60s | security status + per-category finding counts; active-connection count (who-list = view-time re-query) |
| SS cameras | `SS.Camera List` + `SS.Info` | 60s | per-camera status/model/ip/resolution/recording-config; cameras vs license |
| SS events & recordings | `SS.Event CountByCategory`, `Recording.List` | 5m | per-camera events/day + events-today + total recordings/size (aggregate counters) |
| SS license & HomeMode | `SS.License`, `SS.HomeMode` | 5m | licenses used/total/max; armed/disarmed boolean + schedule flags |
| SSH combined probe | `homelab-probe` forced-command script | 5m | `homelab_ssh_*` (up/duration/last-success/host-key) + `homelab_synology_smart_attr{disk,attribute}` + mdstat rebuild + raw upsc vars |
| NFS/mount health | host `statfs` on `/rackstation/*` | 60s | `homelab_synology_mount_up{mount}` + probe-latency (instance-A) |

(During Build, sweep live `load_info` / `Core.System` / `System.Utilization` for any additional useful
fields and fold them in — the set above is the comprehensive core, not necessarily exhaustive.)

## Alert catalog — severity vocab info|warning|critical; hybrid philosophy (LOCKED)

Standing/advisory states (DSM-update-available, security-advisor-not-safe, volume ≥80%, unverified-disk) are
**info** severity (visible, non-paging); genuine problems carry warning/critical. Anomaly rules use the
project's rolling-baseline `clamp_min(K*avg_over_time(...))` idiom with warm-up; absolute-threshold rules
carry the load immediately. The 005-005 user-authored-rule machinery lets the user tune thresholds without
code.

**Metrics rules (`deploy/vmalert/metrics/synology.yaml` + `synology-surveillance.yaml`):**

| Alert | Condition | Severity |
|---|---|---|
| SynologyDown | `homelab_synology_up == 0` (DSM API unreachable) | critical |
| SynologyVolumeDegraded | volume status degraded/crashed | critical |
| SynologyPoolDegraded | pool/RAID `raidStatus` degraded or `normalDevCount < designedDiskCount` | critical |
| SynologyDiskFailed | per-disk `smart_status`/`status` not normal, or `unc`/bad-sector elevated | critical |
| SynologyBackupFailed | a Hyper Backup job's last result = fail | critical |
| SynologyBackupMissing | no successful backup run in N hours (per configured job) | critical |
| SynologyUpsOnBattery | UPS status = on-battery | critical |
| SynologyUpsLowBattery | UPS charge low / runtime below threshold | critical |
| SynologyMountDown | `homelab_synology_mount_up == 0` (root-cause; inhibits dependent ContainerDown) | critical |
| SynologySshHostKeyMismatch | `homelab_ssh_host_key_mismatch{target=synology}` (EPIC-017 surface) | critical |
| SynologyVolumeAlmostFull | volume ≥90% | warning |
| SynologyDiskHighTemp | per-disk temp high (e.g. ≥55 °C) | warning |
| SynologySysHighTemp | `sys_temp` high | warning |
| SynologyScrubError / scrub overdue | scrub state error / last-scrub age high | warning |
| SynologyReplicationLag | snapshot-replication lag high | warning |
| SynologyNoBackupConfigured | HyperBackup running + 0 tasks | warning |
| SynologyHighCpuMem | CPU load / mem sustained high | warning |
| SynologyNeedReboot | `NeedReboot == 1` | warning |
| SynologySshProbeStale | `homelab_ssh_last_success_age{target=synology}` high (EPIC-017 surface) | warning |
| SurveillanceCameraOffline | camera `status != 1` / SS.Log connection-lost | critical |
| SurveillanceCameraNotRecording | camera online but `Event` count ≈0 over window (silent-failure) | warning |
| SurveillanceRecordingStorageLow | Surveillance share free space low | warning |
| SurveillanceLicenseExhausted | `key_used == key_max` | warning |
| SynologyVolumeNearFull (≥80%) | volume ≥80% | info |
| SynologyUpdateAvailable | DSM update available (+`isSecurityVersion` annotation) | info |
| SynologyPackageUpdateAvailable | a package has an update available | info |
| SynologySecurityAdvisorNotSafe | `SecurityScan.Status` != safe (per-category counts in annotation) | info |
| SynologyUnverifiedDisk | pool `has_unverified_disk` attention | info |
| SynologyApiSlow | `api_took_seconds` p95 high | info |

**Logs rules (`deploy/vmalert/logs/synology.yaml`, over the `service="synology-*"` syslog→VL stream):**

| Alert | Pattern | Severity |
|---|---|---|
| SynologyFailedLoginBurst | failed-login lines burst (DSM/SSH/SMB) | warning |
| SynologyAbnormalLogin | geo-anomalous / abnormal-login lines | warning |
| SynologySmartEventLog | SMART warning/error log lines | warning |
| SynologyPackageEventLog | package install/update/failure lines | info |
| SynologyLogErrorRate | general DSM error-rate elevated | info |

(Exact pattern set finalized at STAGE-008-023 against the real forwarded stream — rules validate against real
data, the EPIC-006/007 discipline. Login-event rules are VL-derived per Amendment 5; the geo enrichment
fallback decision is made at the syslog stage once the forwarded line content is known.)

## Events pipeline (DSM syslog → vector → VictoriaLogs)

vector gets a new RFC-syslog source for the Synology (UDP/TCP, non-privileged port; verify the live DSM
syslog RFC format at STAGE-008-020 Design — DSM is NOT CEF). A VRL transform labels lines
`service="synology-<facility>"` (e.g. `synology-auth`/`synology-smart`/`synology-package`/`synology-system`)
so the per-`service` Drain models (EPIC-004 STAGE-004-025+) partition automatically and the `<LogViewer>`
facility filter works. Synology-specific redaction patterns (DSM API tokens, session cookies, photo tokens)
are added to `logs.redact:` so DSM syslog content is redacted at ingest BEFORE VL. The user enables DSM
remote-syslog (Control Panel → Log Center → Log Sending) → `192.168.2.148`. A syslog-vs-metric coverage map
records which categories forward vs. are covered by metric collectors.

## UI structure (two pages under Integrations ▸; observe-only)

**Sidebar (Integrations ▸, alphabetical):** … Home Assistant · Docker · Crons · Pi-hole · **Surveillance** ·
**Synology** · Unifi · Network.

1. **Integrations → Synology:** header status strip (DSM up/down · SystemHealth rule · volume usage · UPS) ;
   storage/volume + disk/SMART grid + RAID/pool ; system/temps/UPS ; backup/replication timeline ; updates/
   packages ; Security Advisor + active-connections ; mount-health ; **SSH-probe state** (last success,
   host-key status, duration — the EPIC-017 render mandate) ; embedded `<LogViewer>` scoped
   `service:"synology-*"`. NO write controls.
2. **Integrations → Surveillance:** camera status grid (online/recording) ; per-camera events/recording
   activity ; recording-storage ; license + HomeMode strip.

## Grafana (two dashboards, both embedded as Metrics tabs)

- **`deploy/grafana/dashboards/synology.json`** — collapsible rows: health/storage, disks/SMART, system/UPS,
  backup/replication, security, mount-health, SSH-probe state. Readability-review pass.
- **`deploy/grafana/dashboards/synology-surveillance.json`** — cameras, events/recordings, license/HomeMode.
  Readability-review pass.

## Scope-outs (deliberately NOT in this epic)

- **SNMP / `snmp_exporter` / Synology MIB** — dropped (Amendment 1; DSM API + SSH cover everything).
- **Write actions** (service/package/container lifecycle, backup-trigger, SMART-test, alert-ack) — observe-
  only (Amendment 6).
- **API log polling** — logs come only via syslog forwarding (Amendment 5).
- **NOPASSWD sudoers on the Synology** — unprivileged SSH only (Amendment 3).
- **Camera-alert de-duplication / suppression vs Unifi/ICMP** — SS camera signals are emitted independently;
  cross-reference/suppression is EPIC-010 (tool-effectiveness) territory (the epic Notes already gesture at
  this). The mount-health → ContainerDown suppression IS built here (Amendment 8); the camera cross-ref is
  not.
- **Host CPU/mem/temps of the MONITOR host** — that is node-level (EPIC-005A), not Synology. (The Synology's
  OWN system metrics ARE in scope, via the DSM API.)
- **Claude auto-fix runbook CONTENT** — engine + `homelab-fixer` user live in EPIC-009; candidate Synology
  runbooks (none high-value for an observe-only critical box) listed there if ever wanted.

## Stage decomposition (33 stages, sequential within waves)

Each stage lands a single small slice and ships independently usable, mirroring the EPIC-005/006/007 wave
shape. Wave order is sequenced for honest data dependencies (collectors before rules; syslog before log
rules; mount-health + Docker-suppression before the rules/UI/dashboards that surface them — Amendment 8).

**Frontend stages (user Desktop+Mobile viewport sign-off required in Refinement):** 008-025, 008-026,
008-027, 008-028, 008-029, plus the Metrics-tab embeds in 008-030 / 008-031. **Instance-A-validation stages
(build here → push → pull into instance A → validate there):** 008-018, 008-019, and the live DSM-side enable
+ real-stream validation of 008-020. All other stages are BACKEND/CLI/YAML/Grafana-JSON → NO user prompt.

### Wave A — Foundation (4)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-001 | DSM API client (`SynologyClient`: v7 session auth, no `session=` param, `verify=False`, re-auth on 119, `took` capture) + `synology_dsm_password` secret + `synology_url` config + lifespan wiring + smoke (`Core.System info`) — ✅ Complete |
| STAGE-008-002 | `integrations/synology/` bundle skeleton + registration (mirror 005-003) — ✅ Complete |
| STAGE-008-003 | Cardinality-cap reuse hook + shared parse / typed-error helpers (`_shared.py`) for Synology collectors — ✅ Complete |
| STAGE-008-004 | SSH `homelab-probe` dedicated-user target config on the EPIC-017 framework (keygen / capture-hostkey / install-instructions / test; no sudoers) — probe BODY lands in Wave D — ✅ Complete |

### Wave B — Storage & system collectors (5)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-005 | Storage collector (`load_info`): per-volume usage/status + per-disk model/temp/`smart_status`/`unc`/`remain_life`/`sb_days_left` — ✅ Complete |
| STAGE-008-006 | Pool & RAID collector: `raidStatus`, normal/designed disk count, rebuild `progress.percent`, `scrubbingStatus`, unverified-disk attention — ✅ Complete |
| STAGE-008-007 | System info + temps collector (`Core.System` v3): model/serial/uptime/`sys_temp`/fan status/`NeedReboot` — ✅ Complete |
| STAGE-008-008 | Utilization collector (`System.Utilization`): CPU load, mem/swap %, per-disk IO, per-NIC rx/tx, NFS OPS+latency — ✅ Complete |
| STAGE-008-009 | UPS collector (`ExternalDevice.UPS`) + SystemHealth `rule` status metric — ✅ Complete |

### Wave C — Protect / maintain collectors (4)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-010 | Hyper Backup collector (`Backup.Task`/`Repository`): per-job last-run/result/size/next-run; graceful-empty + `SynologyNoBackupConfigured` — ✅ Complete |
| STAGE-008-011 | Snapshot Replication collector (`Share.Snapshot`/`Btrfs.Replica.Core`): per-share snapshot age/count + replication lag; graceful-empty (nail down `Btrfs.Replica.Core` read method at Design) — ✅ Complete |
| STAGE-008-012 | Updates & packages collector (`Upgrade.Server check`, `Package`, `Package.Server`) — ✅ Complete |
| STAGE-008-013 | Security Advisor status + active-connections collector (`SecurityScan.Status system_get`, `CurrentConnection`) — ✅ Complete |

### Wave D — SSH probe (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-014 | Combined `homelab-probe` SSH probe (one forced-command script, one 5m run): liveness (`uptime`/`df`) + full per-attribute SMART (`synodisk --smart_info_get` ×8) + raw `/proc/mdstat` rebuild progress + raw `upsc` NUT vars; emits `homelab_ssh_*` + `homelab_synology_smart_attr{disk,attribute}` + mdstat/upsc metrics — ✅ Complete |

### Wave E — Surveillance collectors (3)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-015 | Camera collector (`SS.Camera List` + `SS.Info`): per-camera status/model/ip/resolution/recording-config; cameras vs license — ✅ Complete |
| STAGE-008-016 | Events & recordings collector (`SS.Event CountByCategory`, `Recording.List`): per-camera events/day + events-today + total recordings/size (aggregate counters) — ✅ Complete |
| STAGE-008-017 | License & HomeMode collector (`SS.License`, `SS.HomeMode`): licenses used/total/max; armed/disarmed boolean + schedule flags — ✅ Complete |

### Wave F — Mount health + Docker suppression (2) — instance-A validation
| # | Stage | Theme |
|---|---|---|
| STAGE-008-018 | NFS/`/rackstation` mount-health collector: per-mount non-hanging `statfs`; `homelab_synology_mount_up{mount}` + probe-latency; configurable mount list (instance-A: reads host mount table) — ✅ Complete |
| STAGE-008-019 | Alertmanager inhibit-rules: `mount_up==0` inhibits dependent `ContainerDown` alerts (EPIC-003 cross-cut; built + validated in instance A) |

### Wave G — Syslog pipeline (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-020 | vector Synology RFC-syslog source + `service="synology-*"` labels + Synology redaction patterns + DSM-side remote-syslog enable + syslog-vs-metric coverage map (mirror 007-016; verify DSM syslog format live at Design) |

### Wave H — Alert rules (3)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-021 | vmalert-metrics: Synology core (storage/disk/SMART/RAID/scrub/temp/UPS/system/util/needreboot) |
| STAGE-008-022 | vmalert-metrics: protect/maintain + security + mount + surveillance-integrity (backup, replication, updates, security-status, mount-down, camera-not-recording, recording-storage-low, license-exhausted) — hybrid severity |
| STAGE-008-023 | vmalert-logs over `service="synology-*"`: failed-login burst, abnormal/geo-login, SMART-event, package event, general error-rate (VL-derived; validate against real stream) |

### Wave I — UI (6)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-024 | Backend panel data endpoints (Synology summary/detail + Surveillance summary/detail) — typed rows; OpenAPI regen |
| STAGE-008-025 | Synology page shell + sidebar registration (alphabetical) + header status strip |
| STAGE-008-026 | Synology widgets: storage/volume + disk/SMART grid + RAID/pool + system/temps/UPS + SSH-probe state |
| STAGE-008-027 | Synology widgets: backup/replication timeline + updates/packages + Security Advisor + active-connections + mount-health |
| STAGE-008-028 | Surveillance page shell + registration + camera status grid + license/HomeMode strip |
| STAGE-008-029 | Surveillance widgets: per-camera events/recording activity + recording-storage + embedded `<LogViewer>` (`service:"synology-*"`) on the Synology page |

### Wave J — Grafana (2)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-030 | `synology.json` (health/storage, disks/SMART, system/UPS, backup/replication, security, mount, SSH-probe rows) + Metrics-tab embed + readability pass |
| STAGE-008-031 | `synology-surveillance.json` (cameras, events/recordings, license/homemode) + Metrics-tab embed + readability pass |

### Wave K — Config-loading prerequisite (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-032 | CollectorConfig YAML-loading mechanism: populate `synology_mounts` (+ general collector config); PREREQUISITE for 018 real probing + 019/022 real-data mount alerting — ✅ Complete |

### Wave L — Host-collector hardening (1)
| # | Stage | Theme |
|---|---|---|
| STAGE-008-033 | Host-collector hardening: timeout-wrap host.py's blocking `psutil.disk_usage` probes (non-hanging on wedged NFS; mirrors 018's bounded-executor pattern) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:
- **Concurrency group `synology`** for every DSM-API collector — never DDoS the NAS.
- **Observe-only** — the DSM admin account is used ONLY on read methods; no write paths exist in this epic.
- **DSM password never logged** — assert in tests; read per-request from the TTL-cached resolver, never
  stored on the client.
- **Connection failures handled gracefully** — DSM 5xx/timeout/119 never propagates as our 5xx; collector
  marks failed; failure-budget takes over. Re-auth on 119; self-signed `verify=False`.
- **No API log polling** — all log/event signals arrive via DSM syslog → vector → VL; single source per
  signal (syslog-derived signals are NOT also API-polled).
- **Backup-status is the single most important rule** — false negatives unacceptable. Test the empty-job
  case, the failed case, and the zero-size-but-ran case thoroughly. `SynologyNoBackupConfigured` surfaces the
  "running but 0 tasks" state.
- **Mount-health before container probes** — `homelab_synology_mount_up==0` is the root cause and INHIBITS
  the dependent `ContainerDown` alerts (Alertmanager inhibit-rules).
- **SSH is least-privilege** — the `homelab-probe` dedicated low-priv user via the EPIC-017 forced-command
  framework; no sudo; host-key pinned (mismatch = critical).
- **Graceful degrade** — collectors degrade if optional fields/jobs/UPS absent; cardinality caps prevent
  surprise explosion; emit `homelab_collector_run_*` self-metrics.
- **All internal timestamps UTC.**

## Dependencies

- EPIC-001 (kernel, secrets, collector framework, scheduler, dashboard).
- EPIC-003 (Docker) — the mount-health probe inhibits dependent ContainerDown alerts (STAGE-008-019;
  instance-A validation).
- EPIC-004 (`<LogViewer>` embed contract; vector→VL pipeline; per-`service` Drain models; redaction
  pipeline).
- EPIC-005 (integration-bundle skeleton/registration, cardinality cap, panel/router/sidebar pattern,
  Metrics-tabs embed).
- EPIC-007 (the closest parallel: self-signed API client, direct-API collectors, syslog pipeline, two
  dashboards/pages, observe-only).
- **EPIC-017 (SSH probes) — built FIRST.** The combined Synology SSH probe (STAGE-008-014) is built on its
  scoped `dedicated-user` forced-command framework.

## Notes

- Build sequence: EPIC-017 → EPIC-007 → EPIC-006 → EPIC-008 (whole epics, sequential). All three
  predecessors are COMPLETE.
- The pre-brainstorm 11-stage placeholder is fully superseded by the 31-stage decomposition above
  (recon-grounded, 2026-06-22).
- The user leans toward MORE alerts / MORE detail — reflected in the ~35-alert catalog, the per-disk/
  per-camera granularity, the full per-attribute SMART SSH probe, and the maximal collection surface.
- **Instance-B limitation:** Docker + host-integration are disabled in this checkout. STAGE-008-018/019 (and
  the live DSM-side enable of 008-020) are validated in instance A (push → pull → finish there; the user
  pauses instance-A work during those stages).
- Surveillance cameras also appear as Unifi clients (EPIC-007 registry) + direct ICMP targets — SS signals
  are emitted INDEPENDENTLY; cross-reference/suppression is EPIC-010.
