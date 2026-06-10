# EPIC-005A: Alert backfill for EPICs 001-004

## Status: Not Started (0 / 6 Complete)

## Stages Counter: 0 / 6 Complete

## Current Stage: STAGE-005A-001

## Current Phase: STAGE-005A-001 Design (Not Started)

## Overview

EPICs 001-004 built collectors, the logs pipeline, Docker/cron/heartbeat monitoring, and the
self-monitor — emitting **47 distinct metric families** and a rich log stream. But alerting
coverage lagged the signal coverage: an audit this session found **23 emitted metrics with NO
vmalert rule and 6 more with only partial (static-threshold, no-anomaly) coverage**. The system
collects far more than it alerts on. EPIC-005A is a focused, **alerting-only** epic that backfills
vmalert rules (metrics + logs) for everything already built in 001-004 that simply never got a rule
wired — including the two the user explicitly called out: **log anomalies** and **machine-metric
anomalies**, plus collector health, container lifecycle, cron duration, and build/self-disk gaps.

This epic writes NO new collectors and emits NO new metrics except where a rule provably needs a
metric that is one trivial emission away (flagged per stage). Its deliverables are vmalert rule
files under `deploy/vmalert/{metrics,logs}/` (plus their `__tests__/` shortened-window mirrors),
tuned thresholds, and the dashboards/regression entries that prove each rule fires. It is sequenced
as **005A** (immediately after the EPIC-005 foundation establishes the user-authored MetricsQL
rule machinery and the cardinality/anomaly patterns) because several backfill rules — especially the
machine-metric **anomaly** rules — are best authored on top of, and consistent with, the
rolling-baseline pattern EPIC-005 introduces (STAGE-005-013) and the user-rule surface EPIC-005
extends (STAGE-005-005). Built-in vmalert rules and user-authorable rules must not contradict each
other; doing the backfill right after 005's foundation keeps them coherent.

## Source documents (read before starting any stage)

- Master design spec §4.1 (metric→vmalert→AM flow), §6.2 (metric families), §6.4 (disk budget
  70/85/95 thresholds), §8.2 (severity vocabulary `info|warning|error|critical`), §16 ("all plugins
  observe themselves"; self-monitor-first).
- The completed epic files + stage cards for EPIC-001 (host/self/collector framework), EPIC-002
  (cron/heartbeat), EPIC-003 (Docker), EPIC-004 (logs pipeline + Drain signatures).
- Existing rule conventions: `deploy/vmalert/metrics/*.yaml` (e.g. `docker_probes.yaml`),
  `deploy/vmalert/logs/*.yaml` (e.g. `system.yaml`, `type: vlogs` for LogsQL), and the shortened-window
  test mirrors under `deploy/vmalert/metrics/__tests__/` and `deploy/vmalert/logs/.../`.
- EPIC-005 STAGE-005-013 (rolling-baseline z-score pattern) and STAGE-005-005 (user-authored
  MetricsQL rule machinery) — backfill anomaly rules and user-customizable thresholds must align
  with these.

## Gap analysis (audited 2026-06-10)

An exhaustive audit compared every emitted metric/log signal in 001-004 against the existing
vmalert rule set. Of 47 metric families: ~18 covered, ~6 partial, ~23 uncovered. The uncovered/partial
signals group into six workstreams, each becoming one stage. Severity vocabulary is locked to
`info | warning | error | critical` (spec §8.2). Each rule carries `source_tool: vmalert-metrics`
or `vmalert-logs` and a `target_kind`/`integration` label per the existing convention.

### What is ALREADY covered (do NOT re-add)

- Heartbeat overdue/staleness (EPIC-002 has rules).
- Docker container crash + unhealthy events (EPIC-003 has rules).
- Specific planted-pattern log rules (EPIC-004 `system.yaml` etc.).
- Static CPU/memory >90% thresholds (partial — anomaly variants are the gap).
- Collector-quarantine surfacing (a synthetic `AlertFiringEvent` is emitted on quarantine entry —
  confirm during Design whether a vmalert rule is ALSO wanted, or the synthetic event suffices).

### What is explicitly NOT this epic's job (avoid scope poaching)

- **EPIC-015 (Netdata + comparative shadow rules)** owns ML/k-means behavioral anomaly detection and
  the shadow-rule comparisons. EPIC-005A's "machine-metric anomaly" rules are simple
  **rolling-baseline / z-score / rate-of-change vmalert rules** (the same lightweight pattern as
  EPIC-005-013), NOT Netdata's models. Where a signal genuinely needs ML, note it and defer to 015.
- **EPIC-016 (ISP/WAN)** owns WAN/latency/DNS rules.
- New signals that require a brand-new collector belong to their originating epic, not here. 005A
  only alerts on signals ALREADY emitted by 001-004.

## Stage decomposition (6 stages, parallelizable within the epic)

Each stage is one rule-file workstream: author the rule(s), the shortened-window test mirror, tune
thresholds against real/rig data in Refinement, add regression entries. Stages are independent
(no shared state) and MAY be reordered; listed by descending user-visible value.

| # | Stage | Theme |
|---|---|---|
| STAGE-005A-001 | Host machine anomaly + threshold rules | `host_anomalies.yaml`: CPU/memory rolling-baseline anomaly (beyond the existing static >90%), load-average high (relative to core count), disk-fill projection / free-space thresholds on `homelab_host_disk_bytes`, disk-IO + network saturation on `*_io_bytes_total`/`*_net_bytes_total`, process-count explosion on `homelab_host_processes_total`, reboot detection on `homelab_host_uptime_seconds` reset. Anomaly rules follow the EPIC-005-013 rolling-baseline pattern (NOT Netdata/EPIC-015). |
| STAGE-005A-002 | Collector health rules | `collector_health.yaml`: sustained failure-rate on `homelab_collector_run_failure_total`, run-duration/timeout on `homelab_collector_run_duration_seconds`, last-error-age staleness on `homelab_collector_run_last_error_age_seconds`, and a "collector quarantined" rule (confirm in Design vs the existing synthetic AlertFiringEvent; the quarantine-count metric may not be emitted yet — if a rule needs it, the trivial one-line emission is the documented "alerting-only" exception per Notes). This makes the system's self-observation actually page. |
| STAGE-005A-003 | Container lifecycle rules | `container_lifecycle.yaml`: restart-loop (`homelab_container_restart_count` > N/hour), non-standard exit codes (`homelab_container_last_exit_code` not in {0,137,143}), non-running-without-crash status (`homelab_container_status` exited/paused with no crash context), image-update-available roundup (`homelab_image_update_available`), registry rate-limit exhaustion (`homelab_registry_rate_limit_remaining`), image-update-check-skipped rate. Complements EPIC-003's existing crash/unhealthy rules. |
| STAGE-005A-004 | Cron / heartbeat duration-anomaly rules | `cron_duration.yaml`: unusual run-duration on `homelab_heartbeat_last_duration_seconds` vs a rolling baseline (a cron that suddenly runs 10x longer or finishes suspiciously fast). Heartbeat overdue/staleness is ALREADY covered by EPIC-002 — this fills only the duration-anomaly gap. |
| STAGE-005A-005 | Logging + Drain anomaly rules (LOGS) | `log_anomalies.yaml` (`type: vlogs`): generic per-service error-rate spike (a service suddenly logging far more `error`/`warn` than its baseline — the user's "logging anomalies"), a service-level **log-signature spike** vmalert rule that surfaces the currently backend-only `render_signature_spike_rule` template as a coarse always-on rule, a signature-cardinality-warn rule on `homelab_log_signature_cardinality_warn`, and a Drain-cycle-failure rule. Design picks the coarse-but-shippable spike approach (per-service) vs deferring dynamic per-signature rules. |
| STAGE-005A-006 | Build / config + self-disk rules | `self_and_build.yaml`: self-disk thresholds on `homelab_self_disk_used_pct` at 70% (warning) / 85% (error) / 95% (critical) per spec §6.4 (confirm not already wired), a `homelab_self_disk_shrink_total` increment alert (auto-shrink fired = something's wrong), build-sources-config load-failure on `homelab_build_sources_config_loaded`, and `homelab_build_source_hash_skipped_total` rate. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Every new rule has a shortened-window test mirror** under the appropriate `__tests__/` (or
  logs-test) directory, and an integration test asserting it FIRES on a planted condition and does
  NOT fire on baseline.
- **No duplicate alerts.** Before adding a rule, confirm no existing rule already covers the same
  fingerprint (the audit lists current coverage; re-verify in Design). Overlap with EPIC-002/003
  existing rules is forbidden — fill gaps, don't double-fire.
- **Severity vocabulary respected** — only `info|warning|error|critical`; `source_tool` + a
  `target_kind`/`integration` label on every rule per the existing convention.
- **Anomaly rules are lightweight vmalert (rolling-baseline / rate-of-change), NOT ML** — ML
  behavioral anomaly is EPIC-015's job; do not poach.
- **Thresholds are tuned against real data in Refinement** (prod rig) and documented; a noisy rule is
  a failed rule. Where a sensible default can't be determined, prefer a conservative threshold + a
  note that the user-authored MetricsQL rule machinery (EPIC-005-005) lets the user tighten it.

## Dependencies

- EPIC-001 / 002 / 003 / 004 — the signals being alerted on must already be emitted (they are).
- EPIC-005 foundation: STAGE-005-013 (rolling-baseline anomaly pattern this epic's anomaly rules
  mirror) and STAGE-005-005 (user-authored MetricsQL rule machinery — backfill rules and
  user-customizable rules must stay coherent). 005A is sequenced after the 005 foundation for this
  reason; it does not depend on the HA collectors themselves.
- vmalert (metrics) and vmalert (logs) sidecars + Alertmanager + the alert ingestor (all from
  EPIC-001).

## Notes

- **This epic is "alerting only."** No new collectors. The one permitted exception: if a rule
  provably needs a metric that is a trivial one-line emission from an existing collector (e.g. a
  quarantine count), the stage may add that emission — flagged explicitly in the stage card and kept
  minimal.
- **Why a separate epic and not folded into 001-004?** The signal-vs-alert gap accumulated across
  four epics; collecting the backfill in one auditable place (with consistent threshold tuning and a
  single regression surface) is cleaner than retro-editing four completed epics. It also lets the
  anomaly rules be authored consistently with EPIC-005's freshly-established baseline pattern.
- **Signature-spike decision (STAGE-005A-005):** the `render_signature_spike_rule` template
  currently lives backend-only (no vmalert YAML, no endpoint — noted in the EPIC-004 handoff). This
  epic ships the **coarse, always-on, per-service** spike rule; full dynamic per-signature rule
  wiring (and any frontend-reachable spike-rule authoring) remains a future enhancement, possibly via
  the EPIC-005-005 user-rule machinery.
- **Relationship to EPIC-005:** EPIC-005 builds the user-customizable threshold *machinery*; EPIC-005A
  ships the *built-in default* rules for the existing 001-004 signals. The two are complementary —
  defaults out of the box, customization available.

## Brainstorming session record

Scope locked in this session (2026-06-10) from an exhaustive metrics-vs-rules audit of EPICs 001-004.
Stage Design phases re-verify the current coverage (the audit is a point-in-time snapshot) before
authoring rules; do not add a rule that already exists.
