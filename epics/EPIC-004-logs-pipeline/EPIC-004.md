# EPIC-004: Logs pipeline (vector + VL + Drain + log signature alerts)

## Status: Not Started

## Overview

Mature the logs pipeline beyond the bootstrap from STAGE-001-016. Add Drain log clustering as a periodic in-process job that converts raw lines into "log signatures" (templates + counts), expose those signatures as metrics, and let the user (and future epics) write vmalert rules against the signature metrics for behavioral log anomaly detection. Add per-stream byte/line caps with disk-budget integration. Define a redaction policy for known-sensitive patterns. Build the dashboard's Logs explorer (LogsQL query, live tail, saved queries, "create alert from this query").

After this epic, the logs pipeline is production-quality: capped, observable, queryable, and drives anomaly alerts.

## Docker log requirements (added 2026-05-21)

EPIC-004 MUST specifically address all of the following for docker container logs, IN ADDITION TO the generic Drain-clustering signature work:

1. **Container log ingestion gap fix.** STAGE-001-016 declared per-container stdout/stderr ingestion as a deliverable but shipped with the vector `docker_logs` source's `include_containers` hardcoded to `[]` (empty), and no env-var substitution wired through render-on-boot. Result: container logs are NOT actually flowing into VictoriaLogs. The first stage of EPIC-004 (or a dedicated prerequisite stage) MUST fix this: either (a) default include = all containers on the docker socket, (b) default include = homelab-monitor compose project + opt-in for others via `VECTOR_DOCKER_INCLUDE`, or (c) some other configurable default. The bug is in `deploy/vector/vector.toml.template` + the render-on-boot substitution. Verify post-fix by querying VL for streams keyed on container names (e.g., `service="homelab-grafana"`).

2. **Docker daemon log ingestion verified.** dockerd logs are ALREADY in VL via journald (`service="docker.service"`). Keep this working; add vmalert rules for daemon-specific anomalies (e.g., excessive container-restart events, image-pull failures, OOM kill messages from kernel logged through dockerd).

3. **Per-container exit-code log analysis.** When a container crashes, vector ingests the final stderr + the exit-code metadata. EPIC-004 MUST add a stage that:
   - Correlates container exit-code metadata (from cadvisor's `container_start_time_seconds` reset + the socket collector's `last_exit_code`) with the captured final-N-lines from VL.
   - Produces a metric like `homelab_container_crash{name, exit_code}` that fires when a non-zero exit happens.
   - Emits an alert with the crash context (last 20 lines of stderr) as enrichment.

4. **Per-container error-rate anomalies.** Beyond generic Drain signature counts (already planned in STAGE-004-002), EPIC-004 MUST add per-container rules:
   - Detect error-rate spikes RELATIVE TO each container's own baseline (not a global baseline).
   - Match common error patterns: `ERROR`, `FATAL`, `panic`, `traceback`, `Exception`, HTTP 5xx in log lines, etc.
   - Produce `homelab_container_error_rate{name}` metric + corresponding vmalert rule.
   - Configurable per-container threshold (default: 5x baseline OR 10 errors/min, whichever is more restrictive).

5. **Healthcheck-failure log correlation.** When a container's healthcheck transitions to unhealthy, EPIC-004 MUST fetch the surrounding container log window (60s before + 60s after the unhealthy transition) and attach it to the alert as enrichment. Healthcheck state comes from the socket collector (STAGE-003-003); log fetch is via the existing `VictoriaLogsClient` (introduced in STAGE-002-013).

6. **Pattern analysis for abnormalities.** Beyond Drain signature clustering, EPIC-004 MUST include pattern-based abnormality detection that surfaces things like:
   - **New error patterns** (a signature template never seen before, or seen <N times in last week, suddenly emitted at high rate).
   - **Rare-line spikes** (a specific line — not just a template — appearing N times in a short window when it was previously rare).
   - **Sequence anomalies** (specific log lines appearing in unusual ordering — e.g., `connection reset` immediately followed by `recovering` ratio shifts).
   - **Time-of-day anomalies** (a line that normally appears at 04:00 backup time suddenly appears at 14:00).
   - Whether each of these is in scope for v1 vs deferred to v2 is a Design-phase question per stage; but the EPIC's acceptance criteria explicitly require pattern abnormality detection as a category.

**Cross-references:**
- Container inventory + status + healthcheck source data: STAGE-003-003 (Docker socket collector).
- `VictoriaLogsClient` for VL queries: STAGE-002-013.
- Existing vmalert rules pattern: see `deploy/vmalert/metrics/` and `deploy/vmalert/logs/`.

**Why these are explicit requirements:** generic Drain signature counts (STAGE-004-002 as currently scoped) cover #6's first sub-bullet but not the rest. The user explicitly called out wanting all 6 gaps addressed when EPIC-004 begins.

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
