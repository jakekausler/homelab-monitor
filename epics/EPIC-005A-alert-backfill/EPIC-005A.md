# EPIC-005A: Alert backfill for EPICs 001-004

## Status: In Progress (3 / 9 Complete)

## Stages Counter: 3 / 9 Complete

## Current Stage: STAGE-005A-004

## Current Phase: STAGE-005A-004 Design (Not Started)

## Overview

EPICs 001-004 built collectors, the logs pipeline, Docker/cron/heartbeat monitoring, and the
self-monitor — emitting a rich set of `homelab_*` metric families and a log stream. But alerting
coverage lagged signal coverage: the system collects far more than it alerts on. EPIC-005A backfills
vmalert rules (metrics + logs) for the signals already emitted in 001-004 that never got a rule —
including the two the user explicitly called out: **log anomalies** and **machine-metric anomalies** —
plus host disk-fill, collector health, container lifecycle + resource, cron duration, build/self-disk
health, and a new configurable watched-directory-size feature.

This epic is **predominantly alerting-only** (vmalert rule files + test mirrors + tuned thresholds),
with **two deliberate, user-approved exceptions** that go beyond rules:
1. **STAGE-005A-004** wires probe enablement so the dormant `DockerProbeFailing` alert becomes live
   (EPIC-003 is complete, so all remaining container-monitoring wiring lands here, not back in 003),
   and adds per-container CPU/memory resource alerts (cadvisor).
2. **STAGE-005A-008** adds a NEW watched-directory-size collector + config-driven `:ro` mount
   generation + alerts + a Grafana panel (sizing a directory like `/var` is impossible in pure
   PromQL — it needs a `du`-style collector and host bind-mounts).

It is sequenced as **005A** (after the EPIC-005 foundation) because several anomaly rules are best
authored on top of, and consistent with, the rolling-baseline pattern EPIC-005 introduces
(STAGE-005-013) and the user-rule surface EPIC-005 extends (STAGE-005-005). Built-in vmalert rules
and user-authorable rules must not contradict each other.

> **Scope re-verification note (2026-06-15):** the original 2026-06-10 audit (6 proposed stages, "23
> uncovered / 6 partial metrics") was re-verified against the LIVE rig and corrected. Several of its
> claims were stale or wrong — corrected in the Gap analysis below. Most notably: the originally-named
> "phantom" metrics (`probe_up`, `image_update_check_skipped`, `self_disk_shrink_total`,
> `build_source_hash_skipped_total`) actually DO exist (they emit conditionally); self-disk 70/85/95
> is ALREADY wired (`SelfDiskWarn/Error/Critical`); and the live `DockerProbeFailing` /
> `FixtureHostHighCPU` alerts reference metrics absent in VM (dormant / dead). The stage list was
> restructured 6 → 8 from the verified gap set with the user (2026-06-15). Stage Design phases STILL
> re-verify live coverage before authoring (the live set changes); do not add a rule that already exists.

## Source documents (read before starting any stage)

- Master design spec §4.1 (metric→vmalert→AM flow), §6.2 (metric families), §6.4 (disk budget
  70/85/95 thresholds), §8.2 (severity vocabulary `info|warning|error|critical`), §16 ("all plugins
  observe themselves").
- The completed epic files + stage cards for EPIC-001 (host/self/collector framework), EPIC-002
  (cron/heartbeat), EPIC-003 (Docker + probes + cadvisor), EPIC-004 (logs pipeline + Drain signatures).
- Existing rule conventions: `deploy/vmalert/metrics/*.yaml`, `deploy/vmalert/logs/*.yaml`
  (`type: vlogs` for LogsQL), and the shortened-window test mirrors under
  `deploy/vmalert/metrics/__tests__/` and `deploy/vmalert/logs-test/`.
- EPIC-005 STAGE-005-013 (rolling-baseline z-score pattern) and STAGE-005-005 (user-authored
  MetricsQL rule machinery) — backfill anomaly rules + user-customizable thresholds must align.
- The config-driven-mount machinery for STAGE-005A-008: `scripts/generate-compose-override.sh`,
  `make generate-build-mounts`, `deploy/compose/docker-compose.override.yml` (auto-generated),
  `scripts/host-setup.sh`.

## Gap analysis (re-verified live 2026-06-15)

### Confirmed real gaps (metric EXISTS in VM, no alert) — these become rules

- **Host:** `homelab_host_swap_bytes`, `homelab_host_load_average`, `homelab_host_processes_total`,
  `homelab_host_uptime_seconds` (reboot), `homelab_host_disk_io_bytes_total`,
  `homelab_host_net_bytes_total`; CPU/mem rolling-baseline ANOMALY (beyond the existing static 90%). → 001
- **Host disk-fill:** `homelab_host_disk_bytes{mountpoint}` (`/`, `/storage`) has NO alert at all;
  per-slot `homelab_self_disk_used_bytes`/`_budget_bytes` (vl/vm/sqlite/runbook) only aggregate-alerted. → 002
- **Collector health:** `homelab_collector_run_failure_total`, `_last_error_age_seconds` (both ABSENT
  until first failure — dormant-but-correct), `_duration_seconds` (histogram), silent-death via
  `_success_total` (present). → 003
- **Container:** `homelab_container_restart_count`, `_last_exit_code`, `_status{state}`,
  `homelab_image_update_check_skipped`; per-container CPU/mem (cadvisor `container_*`). → 004
- **Cron duration:** `homelab_heartbeat_last_duration_seconds` (anomaly). → 005
- **Logs/Drain:** `homelab_log_signature_cardinality_warn`, `homelab_drain_cycle_*`,
  `homelab_metric_family_dropped_series`, the coarse per-service signature spike (`signature_spike.yml.tmpl`). → 006
- **Build/self:** `homelab_self_disk_shrink_total`, `homelab_build_sources_config_loaded`,
  `homelab_docker_compose_readable`, `homelab_build_source_hash_skipped_total`. → 007
- **Watched directories (NEW):** no metric exists; sizing a directory needs a new collector + mounts. → 008

### Already covered (do NOT re-add)

- Heartbeat overdue/staleness/failure/flapping (EPIC-002 — live).
- Container crash + unhealthy (EPIC-003/004 — live).
- Planted-pattern + HA log rules; severity-escalation; SSH/OOM (EPIC-004 — live).
- Static host CPU/mem `>90% for 10m` (anomaly variants are the gap, in 001).
- **Self-disk 70/85/95** — `SelfDiskWarn/Error/Critical` ARE live (the original audit's "confirm not
  wired" is resolved: they ARE wired; 007 covers the shrink EVENT + build/config gauges, not the
  aggregate thresholds).
- VictoriaLogs down/latency/disk (EPIC-004 — live; note the vl-slot overlap with 002's per-slot rule).
- New-signature / signature-silent / error-rate-spike / log-stream-budget (EPIC-004 — live).
- **Collector quarantine** — `failure_budget.py` ALREADY synthesizes a `collector_quarantined`
  `AlertFiringEvent`; a `quarantine_count` gauge+rule would duplicate it → DROPPED (user-confirmed).

### Dead / dormant existing alerts (flagged for hygiene in 004)

- `DockerProbeFailing` (`homelab_probe_up == 0`) — correct, but `homelab_probe_up` is EMPTY in VM (no
  probes configured). DORMANT until probes run. STAGE-005A-004 enables probes so it becomes live.
- `FixtureHostHighCPU` (`fixture.yaml`, `fixture_cpu_percent`) — DEAD (metric never exists in prod).
  STAGE-005A-004 (or the epic's first stage) removes / confirms it's a non-loaded test artifact.

### Explicitly NOT this epic's job (avoid scope poaching)

- **EPIC-015 (Netdata + shadow rules)** owns ML/k-means behavioral anomaly + shadow comparisons.
  005A's "machine-metric anomaly" rules are lightweight rolling-baseline / z-score / rate-of-change
  vmalert (EPIC-005-013 pattern), NOT Netdata's models. Where a signal genuinely needs ML, defer to 015.
- **EPIC-016 (ISP/WAN)** owns WAN/latency/DNS rules.

## Stage decomposition (9 stages)

Stages 001-007 are alerting-only (vmalert rule files + test mirrors). Stage 008 is the new-collector +
mounts + panel feature; stage 009 (added 2026-06-15, user request) is a temperature collector + sustained
alerts + a Grafana panel. Stages 001-003, 005-007 are independent and MAY be reordered; 004 (probe
enablement), 008 (collector+mounts), and 009 (temp collector) are the heaviest. Listed roughly by
descending user-visible value; 009 is sequenced last (added after the original 8).

| # | Stage | Theme |
|---|---|---|
| STAGE-005A-001 | Host machine threshold + anomaly rules | ✓ Complete — `host_anomalies.yaml`: CPU/mem critical tiers + rolling-baseline anomaly (EPIC-005-013 pattern), swap-in-use, load-avg vs core count, process-count explosion, reboot detection (uptime reset), optional disk-IO/net saturation. (Disk-fill is 002.) |
| STAGE-005A-002 | Disk-fill rules (host fs + per-slot budgets) | ✓ Complete — `disk_fill.yaml`: host filesystem fill `/` + `/storage` (85/95, for-duration) AND per-slot self-disk `used/budget` (vl/vm/sqlite/runbook); resolve the vl-slot overlap with `vl_health.yaml`. No projection rule (user). |
| STAGE-005A-003 | Collector health rules | ✓ Complete — `collector_health.yaml`: failure-rate, run-duration/timeout, last-error-age, and a silent-death rule (`rate(success_total)==0`). Quarantine DROPPED (existing synthetic alert). Failure/error-age metrics absent until first failure — test mirror plants them. |
| STAGE-005A-004 | Container lifecycle + resource + probe enablement | `container_lifecycle.yaml`: restart-loop, bad exit code, stuck-non-running (whitelist), registry-skip; per-container CPU/mem **share-of-host** (no limits exist → not %); **enable label-based probes** so `DockerProbeFailing` goes live + `DockerProbeSlow`; remove the leaked `fixture.yaml`. EPIC-003 done → all container wiring lands here. |
| STAGE-005A-005 | Cron / heartbeat duration-anomaly rules | `cron_duration.yaml`: run-duration anomaly on `homelab_heartbeat_last_duration_seconds` vs rolling baseline (too-slow; too-fast optional). Staleness already covered (002 of EPIC-002). Smallest stage. |
| STAGE-005A-006 | Logging + Drain anomaly rules | `log_drain_health.yaml` (+ optional logs rule): signature-cardinality-warn, Drain-cycle stall, `metric_family_dropped_series`, and the coarse per-service signature-spike (the user's "log anomalies", from `signature_spike.yml.tmpl`). Must not double-fire `ServiceErrorRateSpike`/`NewLogSignature`. |
| STAGE-005A-007 | Build / config health + self-disk-shrink rules | `self_and_build.yaml`: self-disk auto-shrink fired (`self_disk_shrink_total`), build-sources config load==0, compose unreadable==0, build-source hash skipped. (Aggregate self-disk + per-slot are elsewhere.) Low-severity silent-degradation signals. |
| STAGE-005A-008 | Watched-directory size collector + config-driven `:ro` mounts + alerts + Grafana panel | NEW `homelab_host_directory_bytes{path}` collector (du-with-timeout), `watched_directories` config (default `/tmp` 1G/4G, `/var` 10G/25G), generated `:ro` mounts via the existing override-generator + NEW collision/overlap validation, absolute-threshold alerts, and a Grafana bargauge panel parallel to the disk-mountpoint panel. Backend + host-integration + dashboard; Design may split into 008a/008b. |
| STAGE-005A-009 | Host temperature metrics + sustained alerts + Grafana panel | NEW `homelab_host_temperature_celsius{chip,sensor}` collector (psutil sensors_temperatures — k10temp/nvme/amdgpu confirmed live; readable via default container sysfs, NO compose change). Per-sensor sustained warn/critical threshold rules (CPU/NVMe/GPU) + a per-sensor rolling-baseline thermal anomaly rule, AND a single full-width time-series Grafana panel on host-overview plotting all sensors. Backend + host-integration + dashboard. |

## Cross-stage acceptance criteria

- **Every new rule has a shortened-window test mirror** under the appropriate `__tests__/` (or
  `logs-test/`) directory, plus an integration test asserting it FIRES on a planted condition and is
  silent at baseline. Rules over metrics that are absent-until-event (collector failure, new-signature)
  require the test mirror to PLANT the series.
- **No duplicate alerts.** Re-verify the LIVE rule set in Design (not the stale audit). Overlap with
  existing EPIC-002/003/004 rules is forbidden — fill gaps, don't double-fire. Known overlaps to resolve:
  the per-slot disk rule (002) vs `VictoriaLogsDisk*` on the vl slot; the signature-spike (006) vs
  `ServiceErrorRateSpike`/`NewLogSignature`; the shrink event (007) vs `SelfDiskCritical`.
- **Severity vocabulary** `info|warning|error|critical` only; `source_tool: vmalert-metrics|vmalert-logs`
  + a `target_kind`/`integration` label on every rule.
- **Anomaly rules are lightweight vmalert** (rolling-baseline / rate-of-change), NOT ML (EPIC-015).
- **Thresholds tuned against real prod-rig data in Refinement** and documented; a noisy rule is a failed
  rule. Conservative defaults + a note that EPIC-005-005 user-rule machinery lets the user tighten.
- **Host-integration stages (002 host-fs, 004 probes/resource, 007 config-readable, 008 watched-dirs)
  do BOTH Refinement 3a (synthetic/fixture) AND 3b (real host data on the prod rig).**

## Dependencies

- EPIC-001 / 002 / 003 / 004 — the signals being alerted on are already emitted (verified live).
- EPIC-005 foundation: STAGE-005-013 (rolling-baseline anomaly pattern) and STAGE-005-005
  (user-authored MetricsQL rule machinery) — built-in + user rules must stay coherent.
- vmalert (metrics) + vmalert (logs) sidecars + Alertmanager + alert ingestor (EPIC-001).
- For STAGE-005A-008: the mount-generator machinery + Grafana host-overview dashboard (STAGE-005-041).

## User-approved decisions (locked 2026-06-15)

- **DROP** `collector_quarantine_count` — existing synthetic `collector_quarantined` alert suffices.
- **Probe bug → fix-it in 004** (EPIC-003 is done; probe enablement + alert hygiene land in 005A).
- **Container CPU/mem alerts = share-of-host %** (option b) — no per-container limits exist, so neither
  absolute-bytes nor per-container-% is used; alert on share of host total cores / host total RAM.
- **General host CPU/mem** = threshold-for-duration (existing `for: 10m @ 90%` already satisfies it;
  001 adds critical tiers).
- **Registry rate-limit** = alert on the SKIPPED signal (`image_update_check_skipped`), NOT `rate_limit_remaining`.
- **Self-disk shrink** alert = INCLUDE. **Build/config health** (3 gauges) = INCLUDE all three.
- **Per-volume disk** = INCLUDE both host-filesystem fill AND per-slot budget fill; NO projection rule.
- **Watched directories (NEW)** = config list of dirs with absolute warn/crit limits + a Grafana panel
  parallel to the mount component. Default+test config `/tmp` (1G/4G), `/var` (10G/25G). The directory
  LIST + thresholds are config-driven (`HostCollectorConfig` / generated override); the underlying host
  visibility is a config-driven (env-parameterizable) `:ro` mount applied via the existing
  generate-override + recreate script flow — NOT hardcoded per directory. The mount generator MUST add
  collision/overlap/containment validation (the existing generator has none).

## Notes

- **"Predominantly alerting-only."** The two beyond-rules exceptions (004 probe enablement + resource
  metrics; 008 the watched-dir collector + mounts) are explicit, user-approved, and flagged per stage.
- **Why a separate epic and not folded into 001-004?** The signal-vs-alert gap accumulated across four
  epics; collecting the backfill in one auditable place (consistent threshold tuning, single regression
  surface) is cleaner than retro-editing four completed epics, and lets the anomaly rules align with
  EPIC-005's baseline pattern.
- **Relationship to EPIC-005:** EPIC-005 builds the user-customizable threshold *machinery*; EPIC-005A
  ships the *built-in default* rules for the existing 001-004 signals. Complementary — defaults out of
  the box, customization available.

## Brainstorming session record

Original scope drafted 2026-06-10 from a metrics-vs-rules audit. **Re-verified and restructured
2026-06-15** against the LIVE rig with the user: corrected stale audit claims (phantom metrics that
actually exist; self-disk already wired; dead/dormant probe + fixture alerts), expanded scope per user
direction (full probe enablement, per-container resource alerts, host + per-slot disk-fill, build/config
health, the new watched-directory feature), and re-derived 6 → 8 stages. Stage Design phases re-verify
live coverage before authoring; do not add a rule that already exists.
