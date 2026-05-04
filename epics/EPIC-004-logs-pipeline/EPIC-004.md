# EPIC-004: Logs pipeline (vector + VL + Drain + log signature alerts)

## Status: Not Started

## Overview

Mature the logs pipeline beyond the bootstrap from STAGE-001-016. Add Drain log clustering as a periodic in-process job that converts raw lines into "log signatures" (templates + counts), expose those signatures as metrics, and let the user (and future epics) write vmalert rules against the signature metrics for behavioral log anomaly detection. Add per-stream byte/line caps with disk-budget integration. Define a redaction policy for known-sensitive patterns. Build the dashboard's Logs explorer (LogsQL query, live tail, saved queries, "create alert from this query").

After this epic, the logs pipeline is production-quality: capped, observable, queryable, and drives anomaly alerts.

## Source documents (read before starting any stage)

- Spec §3.1 (alert ingestor uses log signatures), §3.2 (vector + VL sidecars), §6.3 (VictoriaLogs streams + Drain clustering), §6.4 (disk budget and per-stream caps), §9.2 (Logs explorer screen).
- Q9 (logs pipeline decisions: pragmatic mix L1 D1, VictoriaLogs L2 B2, pattern matching + alerting L3).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-004-001 | Per-stream byte/line caps with disk-budget integration (extends STAGE-001-016's basic throttle); `log_stream_budget` collector + vmalert rule from STAGE-001-018 fully integrated |
| STAGE-004-002 | Drain clustering job: periodic (5min interval) batch over recent VL data; produces signature templates + counts; emits `homelab_log_signature_count{template_hash, service}` metrics |
| STAGE-004-003 | Drain signature catalog UI: "Signatures" tab inside the Logs screen; user can label signatures, suppress noise, mark as "expected" |
| STAGE-004-004 | Signature-anomaly vmalert rules: rules generated from baselines (count > N×rolling-baseline OR new signature seen in last X) |
| STAGE-004-005 | Logs explorer UI: full-text/LogsQL query, time range, live tail, saved queries; integrates with the signature catalog for "show me lines matching this signature" |
| STAGE-004-006 | Redaction pipeline: vector transforms strip well-known sensitive patterns (bearer tokens, JWTs, passwords in URLs, AWS keys); audit log records what was redacted (counts only, never the redacted value) |
| STAGE-004-007 | "Create alert from query" UX: user composes a LogsQL query in the explorer, clicks "Alert when this fires", produces a vmalert rule via a guided form; rule is committed to `deploy/vmalert/logs/` (or stored in DB and rendered to file at runtime — Design phase decides) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **No raw log line that contains a known-sensitive pattern leaves the pipeline un-redacted** — assert in tests with planted credentials.
- **Drain clustering memory + CPU bounded** by configurable batch size; over budget = abort batch, alert.
- **User-created alert rules from the UI are reviewable** before activation: the UI shows the resulting rule YAML and asks for confirmation before persisting.

## Dependencies

- EPIC-001 (vector, VL, vmalert-logs in place).
- EPIC-002 not strictly required, but the cron-status log parser from STAGE-002-004 will use the same signature pipeline.

## Notes

- Drain reference implementation: paper "Drain: An Online Log Parsing Approach with Fixed Depth Tree" (He et al.). Multiple Python implementations exist; pick one with active maintenance during STAGE-004-002 Design phase.
- Redaction policy lives in `homelab-monitor.yaml` under `logs.redact:`. Default rules ship with the public release; the host-overrides repo can add private patterns (e.g., regex for the user's specific API tokens that aren't yet in the secrets store).
- The "create alert from query" feature is the first time end-users (rather than developers) will be authoring alert rules. UX must enforce sane defaults (severity, group_by, group_interval) and warn on patterns that historically produce false positives.
