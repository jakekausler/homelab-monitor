# EPIC-020: Forensic Timeline (cross-source incident view + side-by-side comparison + per-line bookmarking + cross-log correlation)

## Status: Not Started (placeholder)

## Overview

Cross-source forensic view that lets the user investigate "what happened around timestamp X" across ALL signal types — logs, metrics annotations, alert events, cron runs, container state changes, healthcheck transitions — in a single unified timeline. Plus side-by-side time-range comparison ("show errors today next to errors yesterday at the same time"), per-line bookmarking, and per-service "personality" baselines (volume + severity distribution over time-of-day).

This epic was deliberately split out from EPIC-004 (Logs pipeline) during the 2026-05-28 brainstorming session. The Logs Explorer (EPIC-004 STAGES 004-010..024) ships a great LogsQL surface with sidebar / saved queries / histogram / field inspector / live tail; this epic builds the cross-source incident view on top of that foundation once it lands.

## Source documents

- Master design spec §9.2 (logs screen) — implies forensic-style cross-source investigation but defers concrete UX
- EPIC-004 brainstorming session 2026-05-28 (locked the split: ALL forensic features deferred here, not in EPIC-004)
- Future input from the user when they hit a real-world incident that benefits from this view

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-020-001 | Cross-source event collector: unified timeline backend that aggregates events from: VictoriaLogs (lines), VictoriaMetrics (annotations + alert-fired/resolved events), SQLite (cron run boundaries, container lifecycle, healthcheck transitions). Returns a single sorted stream. |
| STAGE-020-002 | Forensic Timeline UI: page at `/timeline` showing the unified event stream filtered by time range. Each event row has source-type-specific icon + click-to-expand detail. |
| STAGE-020-003 | "Investigate around timestamp X" entry point: click any timestamp in the Logs Explorer, alert detail, cron run viewer, container overview, etc. → opens timeline at ±60s window. |
| STAGE-020-004 | Side-by-side time-range comparison: two `<LogViewer>` panes (or two timelines) at different ranges, same filter. Useful for "errors today vs yesterday." |
| STAGE-020-005 | Per-line bookmarking: mark a log line (or any timeline event) with a label; recall via a "Bookmarks" panel. Persisted in SQLite. |
| STAGE-020-006 | Per-service "personality" baseline collector: track each service's typical hourly volume + severity distribution. Surface as a heatmap on the service's detail page. |
| STAGE-020-007 | Personality-drift alert: vmalert rule firing when a service's volume OR severity-distribution shifts dramatically from baseline. Different from EPIC-004 STAGE-004-036's per-signature spike — this catches "service is unusually noisy/quiet overall" patterns. |
| STAGE-020-008 | Cross-log correlation alert enrichment: when a high-severity alert fires, fetch lines from ALL services in a ±60s window around the alert timestamp (not just the related service); attach as enrichment. The "lots of context across services" view. Most useful when Claude integration epic is also live, so Claude can synthesize a hypothesis from the cross-service slice. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Timeline backend is performance-conscious**: aggregating across 3 stores (VL + VM + SQLite) at high time-resolution can be expensive. Cap the time range, cache results, paginate.
- **No new redundant ingest paths**: timeline READS from existing stores; doesn't create a fourth data source.
- **Reuse `<LogViewer>` component from EPIC-004 STAGE-004-003** for any log-line rendering inside the timeline; no parallel UI.

## Dependencies

- EPIC-001 (kernel, alerts, audit log).
- **EPIC-004 (logs pipeline)** — REQUIRED. Forensic Timeline builds on top of the Logs Explorer, `<LogViewer>` component, Drain signature catalog, and the converged `LogLine` shape.
- EPIC-002 (cron runs as timeline event source).
- EPIC-003 (container lifecycle + healthcheck transitions as timeline event source).
- (Future) Claude integration epic — cross-log correlation (STAGE-020-008) yields the most value when Claude can synthesize a hypothesis across the cross-service slice. Without Claude, the user gets a wall of logs to read manually.

## Notes

- This epic exists as a **placeholder** pending real-world demand. The 2026-05-28 brainstorming session for EPIC-004 considered these features and deliberately deferred them to keep EPIC-004 focused on logs-as-such. When the user hits an incident where forensic timeline would have saved hours, this epic gets prioritized.
- Specific deferrals from EPIC-004 captured here (mapping):
  - **Side-by-side time-range comparison** (EPIC-004 brainstorm Q11c) → STAGE-020-004
  - **Per-line bookmarking** (EPIC-004 brainstorm Q11c) → STAGE-020-005
  - **Forensic timeline** (EPIC-004 brainstorm L12) → STAGES 020-001, 020-002, 020-003
  - **Cross-log correlation** (EPIC-004 brainstorm L10) → STAGE-020-008
  - **Per-service personality baseline** (EPIC-004 brainstorm L11) → STAGES 020-006, 020-007
- This epic is NOT BLOCKING any other epic. EPICs 005..019 can ship without it. The user's homelab is usable; forensic-timeline is "next-level investigation comfort."
