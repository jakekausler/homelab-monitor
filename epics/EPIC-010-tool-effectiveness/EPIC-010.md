# EPIC-010: Tool effectiveness analyzer + scorecards + recommendations

## Status: Not Started

## Overview

Build the tool-effectiveness analyzer subsystem: tracks per-tool alert volume, action rates, dedup overlap, unique-detection share, runs comparative shadow rules where applicable, generates auto-recommendations after configurable observation windows, and surfaces all of this on the Tool Analysis screen.

This epic operationalizes the user's instruction (Q17 → option D) to know which integrated tools are pulling their weight. After this epic, the system can generate "Netdata caught 0 unique alerts in 90 days; consider disabling for metric set X" or "vmalert-baseline rule R has 95% noise rate; tune or remove" recommendations.

## Source documents

- Spec §3.1 (tool-effectiveness analyzer), §4.7 (data flow), §6.1 (`alert_outcomes`, `tool_scorecards` tables), §9.2 (Tool analysis screen).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-010-001 | Outcome capture: ensure every alert ingested in EPIC-001 onward gets an outcome row when the user acks/dismisses, when auto-fix runs, or when the alert auto-resolves. The `alert_outcomes` table from STAGE-001-013 already exists; this stage tightens the writes |
| STAGE-010-002 | Per-tool aggregation job (runs nightly): computes for each `source_tool` over a configurable window (7d / 30d / 90d): alerts_emitted, action_rate (acked / dismissed / auto_fixed), unique-share (alerts that no other tool caught), dedup_overlap (alerts where multiple tools caught the same fingerprint) |
| STAGE-010-003 | Shadow-rule framework: pairs of detectors (e.g., a Netdata anomaly subscription + a vmalert baseline rule on the same metric) run in parallel with `source_tool` set to differ; the analyzer compares hit/miss/false-positive rates between pairs |
| STAGE-010-004 | First shadow-rule pair: vmalert rolling-baseline rule for `homelab_host_cpu_percent` vs Netdata anomaly subscription for the same metric. Surfaced as a comparison panel in the Tool Analysis screen |
| STAGE-010-005 | Recommendation engine: rules-as-code that generate human-readable recommendations from the scorecards (e.g., "if a tool has 0 unique-share over 90d AND 95%+ dedup_overlap, recommend disabling"). Ships with a starter set of rules; user can edit |
| STAGE-010-006 | Tool Analysis UI: per-tool cards (alerts emitted sparkline, action-rate bar, unique-share percentage), comparison panels for shadow pairs, recommendations inbox with "Apply" / "Dismiss" actions |
| STAGE-010-007 | "Apply" action: implementing a recommendation (e.g., disable a vmalert rule, unsubscribe a Netdata anomaly, deactivate a collector) is itself an action requiring confirm-on-destructive and audit |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **The analyzer never deletes data.** Recommendations propose actions; the user approves and the action is taken via the same audit-log + confirm-on-destructive paths as everything else.
- **Computations are deterministic given inputs** — recompute on demand returns the same scorecards.
- **Scorecards stored in `tool_scorecards`** survive restarts; expiring windows roll forward.

## Dependencies

- EPIC-001 (alert ingestor + outcomes table).
- EPIC-009 (auto-fix outcomes feed the analyzer).
- EPIC-015 (Netdata) — first shadow-rule pair needs Netdata to exist; if EPIC-015 ships before EPIC-010, the pair is enabled day-one in this epic; otherwise it lands as a follow-on stage in EPIC-015.

## Notes

- "Unique share" definition is precise: an alert is unique to tool T if T's `source_tool` is the ONLY one that emitted an alert with the same fingerprint within ±group_interval. (Group_interval is Alertmanager's, default 5m.)
- The analyzer's nightly job has its own concurrency group `analyzer` and runs at low CPU priority (nice/ionice).
- Recommendations are advisory by default. The user-controlled "auto-apply low-risk recommendations" mode is deferred to a future epic — for now, every recommendation requires explicit user action.
