# EPIC-004: Logs pipeline (Drain clustering + Logs Explorer + signature anomaly alerts + per-store thresholds)

## Status: In Progress

## Stages Counter: 42 / 50 Complete

## Current Stage: STAGE-004-038

## Current Phase: STAGE-004-038 Design / Not Started

## Overview

Mature the logs pipeline beyond the bootstrap delivered in EPIC-001. This epic delivers the production-quality logs surface: a unified `<LogViewer>` shared across all log-display surfaces, a top-level Logs Explorer at `/logs` with paginated LogsQL queries + custom range + advanced LogsQL editor + stream picker + saved queries + query history + field inspector + nested JSONL extraction + histogram + export + live tail, Drain log clustering (drain3) with a Signature Catalog, four kinds of signature anomaly rules, three kinds of log-windowed alert enrichment (container crash, healthcheck failure, cron failure), a vector-layer redaction pipeline, container label enrichment, multi-line stitching, severity escalation, budget warnings, VL backend health alerts, a global retention settings UI, and a user-curated rule-authoring flow with persistence.

After this epic, the logs pipeline is production-quality: redacted, capped, observable, queryable, anomaly-aware, and drives user-authored alerts. The deferred-from-STAGE-003-011 cursor pagination + custom datetime range picker land naturally as part of the foundation wave.

**Brainstorming reference:** 2026-05-28 brainstorming session locked the initial 44-stage decomposition; 2 additional stages (STAGE-004-016A and STAGE-004-016B) were added post-brainstorm (2026-06-03). Each stage file in this directory carries the locked D-* decisions inherited from that session.

## Source documents (read before starting any stage)

- Master design spec §3.1 (alert ingestor + log signature consumption), §3.2 (vector + VL + vmalert-logs sidecars), §6.3 (VictoriaLogs streams + Drain clustering), §6.4 (disk budget + per-stream caps), §9.2 (Logs explorer screen + signature catalog + inventory detail "related logs").
- Q9 (logs pipeline decisions): pragmatic mix of journald/docker/syslog ingestion; VictoriaLogs as the store; pattern-matching + Drain-derived metrics + LogsQL-rule alerting.

## Brainstormed architecture (2026-05-28)

### Unification approach

- **Single `<LogViewer>` component** consumed by docker per-container viewer, cron per-run viewer, and the new Explorer at `/logs`. Caller provides a `useLogs` hook; component handles all rendering states (loading / error / empty / unavailable / truncated / live). Explicit embedding contract documented for future detail pages (HA / Pi-hole / Unifi / Synology / probe-detail / alert-detail).
- **Three coexisting endpoints**: `/api/integrations/docker/containers/{name}/logs` (docker context), `/api/crons/{fp}/runs/{run_id}/log` (cron context), `/api/logs/query` (generic LogsQL). All three return the same converged `LogLine` shape.
- **Top-level `/logs` route** for the Explorer; "Open in Explorer" deep-links from per-context viewers pre-fill filters.

### Drain runtime model

- **`drain3` library** (IBM, MIT-licensed) with custom `SqlitePersistence` backend.
- **Periodic batch consumer** every 5 minutes (configurable via `HOMELAB_MONITOR_DRAIN_INTERVAL_S`), with manual refresh trigger via API + UI.
- **Per-`service` model granularity** (one drain3 tree per service); cron specialcased to `cron:<fingerprint>` via override hook. New sources (Synology / UDM / HA file-tail) automatically get their own model.
- **SQLite-persisted** model state (`drain_models` table); cycle cursor stored per-model.

### Anomaly categorization

Every signature-anomaly alert carries `category: log-anomaly` + `anomaly_kind: {new_signature | signature_spike | error_rate_spike | signature_silent}` so downstream consumers (Karma, notification routes, the homelab-monitor UI, the future Claude integration epic) can route/render appropriately.

### Spike-detection algorithm

- **7-day rolling baseline** (e.g., `avg_over_time(...)[7d:5m]`)
- **1-hour static fallback** during cold-start (when signature has < 7 days of history)
- **Multiplier configurable per rule** (default 5×); window configurable (default 5 min); both editable via the create-alert-from-signature UI

### Alert authoring

- **User-curated v1** — Drain catalog + Saved Queries each have "Create alert" entry points that pre-fill the alert-authoring modal with sensible defaults. No auto-generation in v1.
- **Auto-generation deferred to Claude integration epic.**
- **Rule persistence**: SQLite `log_user_rules` table + render-on-boot into `deploy/vmalert/logs/user-rules/` (and `deploy/vmalert/metrics/user-rules/` for metricsql rules).

### Redaction pipeline

- **Vector VRL transforms** strip the bearer-token / JWT / password-in-URL / AWS-key / generic-api-key default patterns BEFORE log lines hit VictoriaLogs. Patterns come from `homelab-monitor.yaml` under `logs.redact:`, rendered into vector.toml at boot.
- **Audit metric** (`vector_redactions_total{pattern_type}`) — counts only, never values, per spec §3.1.
- **Synology / Unifi-specific patterns** forward-referenced in EPIC-007 and EPIC-008 (added when those syslog sources land).

### Per-store thresholds (no cross-store coordinator)

VL has independent thresholds (`HOMELAB_MONITOR_VL_DISK_WARN_PCT=70` / `CRIT_PCT=85`) surfaced in the Settings/Logs page and driving vmalert rules. **Cross-store auto-shrink coordinator is intentionally NOT being built** (locked decision); each of VM and SQLite should have mirroring per-store thresholds, added in their own epics or in EPIC-014 (self-monitor).

## Stage decomposition (45 stages, sequential)

Stages MUST be implemented in order. No parallelization. Each stage lands a single small slice and ships independently usable.

### Foundation wave (S01-S09)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-001 | Multi-line log handling | Vector multiline codec stitches tracebacks / stack traces into single events. Lands first so all downstream stages get clean events. ✅ Complete |
| STAGE-004-002 | Backend `LogLine` shape convergence | All 3 endpoints (docker / cron / generic) return one converged `LogLine` shape. Existing UI continues to work. ✅ Complete |
| STAGE-004-003 | `<LogViewer>` extraction + cron/docker viewer refactor | Shared component; embedding contract documented for future detail pages. ✅ Complete |
| STAGE-004-004 | Container label enrichment | `compose_project`, `compose_service`, image labels as top-level VL fields. ✅ Complete |
| STAGE-004-004A | Docker log severity-level extraction | Parse error/warn tokens from docker log messages; enable severity tinting in `<LogViewer>`. ✅ Complete |
| STAGE-004-005 | Cron fingerprint enrichment | hmrun transform adds `cron_fingerprint`; Drain consumer's model-key override hook uses it. | ✅ Complete |
| STAGE-004-006 | Redaction pipeline | Vector VRL + audit metric + yaml-driven patterns. | ✅ Complete |
| STAGE-004-007 | Cursor pagination | All 3 endpoints + `<LogViewer>`. Fixes STAGE-003-011's D-DEFER-PAGINATION. | ✅ Complete |
| STAGE-004-008 | Custom datetime range picker | All 3 viewer surfaces. Fixes STAGE-003-011's D-DEFER-CUSTOM-RANGE. | ✅ Complete |
| STAGE-004-009 | Local-time rendering with UTC toggle | Applies via `<LogViewer>`. | ✅ Complete |

### Explorer wave (S10-S22)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-010 | Logs Explorer skeleton at `/logs` | Plain-text search + range + paginated results via `<LogViewer>`. | ✅ Complete |
| STAGE-004-011 | LogsQL advanced mode + syntax highlighting | "Advanced (LogsQL)" toggle; CodeMirror-based editor with basic token highlighting. | ✅ Complete |
| STAGE-004-012 | Stream picker sidebar | Distinct services with line counts; click injects filter via separate state (composes with LogsQL). | ✅ Complete |
| STAGE-004-012A | Service source_type field + grouped/collapsible stream picker | Explicit `source_type` label at Vector ingest (docker/systemd/cron/unknown); refactor stream picker into collapsible per-type sections with select-all/none. Inserted (user request) — builds on STAGE-004-012. | ✅ Complete |
| STAGE-004-013 | Saved queries | SQLite-backed; named queries restore full Explorer state. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-014 | Query history | Last 20 executed queries (localStorage v1; SQLite later if cross-device needed). Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-015 | State persistence | Last query / range / scroll position across navigation. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-016 | Field inspector | Click a line → side panel with parsed fields + copy + add-to-filter. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-016A | LogsQL structured field filters | Add-to-filter uses structured operators (host:"value", severity:"error") for non-message fields instead of `_msg` substring. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-016B | JSON message drill-down in field inspector | Recursive collapsible tree for JSON messages; suppress duplicate flat-key sibling rows. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-017 | Generic nested-field extraction at ingest | ✅ Complete |
| STAGE-004-018 | Filter-scope-aware field discovery | "Available fields" panel shows only fields present in current scope (sample-based, cached). Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-018A | LogsQL editor autocomplete + field suggestions | In-editor code completion in advanced mode; LogsQL keywords + field names + sample values from scope-aware discovery (STAGE-004-018). Inserted (user request) — autocomplete deferred from STAGE-004-011. |
| STAGE-004-018B | Configurable visible columns in logs results | Add/remove/reorder result columns (service, host, severity, discovered fields) to separate line types in the unified view; opt-in `<LogViewer>` columns. Inserted (user request). |
| STAGE-004-019 | Histogram of line counts | Stacked-by-severity bar chart above results; click bucket to narrow range. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-020 | Log-line export | Download matching lines as .txt or .json with streamed backend + cap. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-021 | "Open in Explorer" deep-link | Buttons on docker + cron viewers; helper documented for future detail pages. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-022 | Global retention settings UI | `/settings/logs` page showing VL retention + thresholds; per-store, no coordinator. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |

### Live tail wave (S23-S24)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-023 | Backend SSE endpoint | Server-side streaming from VL; connection caps + backpressure + per-conn metrics. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-024 | Frontend tail mode | Explorer consumes SSE; auto-scroll sticky behavior; pause/stop controls. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |

### Drain wave (S25-S29)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-025 | drain3 wrapper + SQLite persistence | `DrainEngine` + `SqlitePersistence`; model-key override hook with cron special case. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-026 | Periodic batch consumer service | Runs every 5 min (configurable); cursor per model; partial-cycle handling. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-027 | Metrics emission + manual refresh API | `homelab_log_signature_count`, `_first_seen_ts`, `_total`; `POST /api/logs/signatures/refresh`. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-028 | Signature catalog backend + list/drill-in UI | Tab on `/logs`; label / suppress / mark-expected / search / sample-lines / Open-in-Explorer. Design ✅ Build ✅ Refinement ✅ Finalize ✅ | Complete |
| STAGE-004-029 | Signature annotations | Timestamped notes per signature; chronological list in drill-in panel. Design ✅ Build ✅ Refinement ✅ Finalize ✅ | Complete |

### Diagnostics (S30)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-030 | Drain models dump endpoint + UI viewer | Read-only diagnostics surface; last-cycle stats panel on Signatures tab. Design ✅ Build ✅ Refinement ✅ Finalize ✅ | Complete |

### Anomaly wave — shared infrastructure (S31)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-031 | `LogWindowFetcher` shared service module | Used by crash / healthcheck / cron correlation; cached, capped, degrades gracefully on VL error. Design ✅ Build ✅ Refinement ✅ Finalize ✅ | Complete |
| STAGE-004-031A | "Show surrounding logs" in the Explorer | From a log line, fetch ~100 lines before/after (scope: all services or only this service) via LogWindowFetcher; step 3 of the model→explorer→context debug flow. Depends on STAGE-031. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |

### Anomaly wave — rules (S32-S39)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-032 | Container crash log correlation | `homelab_container_crash` metric + alert annotation + UI render. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-033 | Healthcheck-failure log enrichment | 60s window attached to unhealthy alerts. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-034 | Cron run failure log correlation | Last N lines of hmrun output enriched into cron-failed alerts. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-035 | Anomaly Type A: New signature detected | Rules + first_seen metric + suppression integration. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-036 | Anomaly Type B: Signature count spike vs baseline | 7d rolling baseline + 1h cold-start fallback; template rendered per signature via STAGE-004-044. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-037 | Anomaly Type C: Service-wide error rate spike | `homelab_container_error_rate` metric + rule; satisfies docker req #4. Design ✅ Build ✅ Refinement ✅ Finalize ✅ |
| STAGE-004-038 | Anomaly Type D: Signature went silent | Expected-silence allowlist (always / cron / window kinds). |
| STAGE-004-039 | Severity escalation rules (L1) | Any critical-severity line triggers alert; per-service exclude overrides. |

### Operational alerts (S40-S41)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-040 | Throttle/budget alerts (L2) | Approaching budget / vector throttling / unusual rate. |
| STAGE-004-041 | VL backend health alerts (L3) | VL down / latency / disk-warn / disk-crit; mirrors per-store threshold pattern. |

### Alert authoring (S42-S44)

| # | Stage | Theme |
|---|---|---|
| STAGE-004-042 | Rule persistence model | SQLite `log_user_rules` + render-on-boot into vmalert directories. |
| STAGE-004-043 | Create-alert-from-query UX | Guided form with YAML preview launching from Explorer. |
| STAGE-004-044 | Create-alert-from-signature + saved-query shortcuts | L8 merged in; both pre-fill the shared modal. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **No raw log line that contains a known-sensitive pattern leaves the pipeline un-redacted** — STAGE-004-006 asserts with planted credentials. Subsequent UI stages MUST NOT add a path that exposes pre-redaction content.
- **Drain clustering memory + CPU bounded** by `batch_max_lines` (default 50,000); over-budget cycles are marked `'partial'`, not failed, and resume next cycle.
- **All anomaly alerts carry `category: log-anomaly` and `anomaly_kind: ...`** so downstream consumers can route/render uniformly.
- **User-created alert rules from the UI are reviewable before activation** — YAML preview in the modal; explicit Save click required.
- **`<LogViewer>` is the only component rendering log line lists in the app** — embedding contract documented; future detail pages (HA / Pi-hole / Unifi / Synology) consume it without rebuilding.

## Out of Scope (explicitly considered and declined; routing for deferred items below)

1. **Cross-store disk-budget orchestration coordinator** — intentionally NOT being built; each of VM / VL / SQLite has independent thresholds (locked in brainstorming 2026-05-28 Q13c).
2. **Per-stream retention overrides** — global retention only.
3. **Per-signature retention** — global retention only.
4. **Log forwarding to external sinks (Loki, S3 archive)** — not in master spec.
5. **Log compression / cold storage tiers** — not in master spec.
6. **Log query rate limiting** — single-user system.
7. **Backfill of old logs from external sources** — not in master spec.
8. **Audit trail of WHO ran what log query** — single-user system.
9. **Log sampling beyond vector throttle** — existing 50 lines/sec/service throttle is sufficient.
10. **Side-by-side time-range comparison** — deferred to the **Forensic Timeline epic** (new epic, created post-EPIC-019).
11. **Per-line bookmarking** — deferred to the **Forensic Timeline epic**.
12. **Forensic timeline (cross-source incident view)** — own dedicated epic (new; placeholder to be created).
13. **Per-service "personality" baseline (volume + severity over time-of-day)** — deferred to the **Forensic Timeline epic**.
14. **Multi-user log access controls / RBAC** — single-user system.
15. **Bulk operations on signatures (multi-select suppress)** — deferred until user demand surfaces.
16. **Auto-generated alert rules from signatures** — deferred to **EPIC-009 (Auto-fix)** / future Claude integration epic. EPIC-009 currently scoped to runbook-driven remediation; the Claude integration that adds Drain-pattern→alert-rule auto-generation will be its own epic.
17. **Live tail in docker / cron per-context viewers** — Explorer-only in v1; can be retrofitted later via `<LogViewer>` opt-in prop.
18. **Tree view of nested fields in Field Inspector** — flat dotted paths only.

## Dependencies

- EPIC-001 (vector, VL, vmalert-logs in place); cron-status log parser from STAGE-002-004 will hook the Drain pipeline once available.
- EPIC-002 (cron run logs + cron run viewer; cron fingerprint enrichment depends on STAGE-002-* fingerprint definitions).
- EPIC-003 (docker container drill-down; STAGE-004-021 "Open in Explorer" button + STAGE-004-032/033 enrichments hook into ContainerOverviewTab + AlertDetailPage).

## Notes

- **drain3 reference**: paper "Drain: An Online Log Parsing Approach with Fixed Depth Tree" (He et al.). IBM's `drain3` library is locked.
- **Redaction policy** lives in `homelab-monitor.yaml` under `logs.redact:`. Default v1 patterns ship with the public release; host-specific patterns (Synology API tokens, UDM bearer tokens) added via EPIC-007 and EPIC-008 in their own stages.
- **Anomaly category labels** are designed for future Claude-integration auto-suggestion: when that epic ships, every log-anomaly alert is already tagged with `anomaly_kind` and a deep-link to the catalog/Explorer for Claude to reason about.
- **50 stages is intentionally fine-grained.** Each stage is sized to land in one session. The brainstorming session 2026-05-28 explicitly decided "small slices, ship one feature per stage" over fewer larger stages. Two additional stages (STAGE-004-016A and STAGE-004-016B) were inserted post-brainstorm to address structured field filters and JSON message drill-down.

## Brainstorming session record

The full set of locked decisions was captured in stage files' `Locked Design Decisions` sections. Authoritative reference: the brainstorm conversation of 2026-05-28 (preserved in conversation logs). Stage Design phases inherit these decisions; do not re-litigate.
