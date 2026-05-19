# Cron Run History & Run-Log Viewing — Design Spec

**Date:** 2026-05-19
**Status:** Approved (brainstorming complete; awaiting spec review before implementation planning)
**Epic:** EPIC-002 (Heartbeat receiver + cron registry + cron auto-discovery) — **re-opened** with appended stages STAGE-002-011 … STAGE-002-015
**Supersedes:** nothing — this is a new capability layered on the shipped EPIC-002 cron-monitoring subsystem.

---

## 1. Purpose & Problem Statement

EPIC-002 shipped cron *health* monitoring: a heartbeat receiver, cron registry, auto-discovery,
B-mode log-scrape, wrapper install/remove, and vmalert alerting. Today the system can tell the
user a cron is **stale, late, or failing** — but it cannot show the user **what an individual
cron run actually did**. The `heartbeats_state` table holds only the *last* run's
timestamp/duration/exit-code plus aggregate counters; individual run history is discarded, and
no stdout/stderr is captured or viewable anywhere in the product.

This spec adds three layered capabilities:

1. **Run history** — a browsable, per-invocation record for every discovered cron.
2. **Run-log viewing** — the actual stdout/stderr text of an individual run.
3. **Anomaly detection (v1)** — rule-based heuristic flags surfacing runs that behaved
   differently from a cron's norm (slower, unexpected exit code, output-size deviation,
   unexpectedly empty), including runs that nominally "succeeded".

### Driving use cases (all three, layered)

- **Post-failure debugging** — when a cron fails, open its detail page and read what *that
  specific run* printed, the way one would read `journalctl` for that job.
- **Browse run history** — see a timeline of the last N runs (timestamps, durations, exit
  codes, output) and cycle through them, whether or not anything failed.
- **Spot anomalies over time** — proactively flag runs that deviate from the cron's baseline.

History browsing is the foundation; failure-debugging is the immediate payoff; anomaly
detection is a layer on top of the stored history.

### Confirmed: not previously planned

A codebase + epic + design-spec investigation confirmed run-log viewing / run history /
anomaly detection on cron logs is **not scoped by any existing epic or stage**. EPIC-004
(Logs Pipeline, Not Started) has a *generic* logs explorer and Drain clustering, but nothing
cron-run-specific. This is genuinely new work.

---

## 2. Scope

### In scope

- A new `cron_runs` table (one row per cron invocation) + alembic migration.
- Run history for **all discovered crons** — both A-mode (wrapper-installed) and B-mode
  (observe-only / unwrapped).
- Run-log *text* viewing:
  - **A-mode crons:** high-fidelity — the upgraded wrapper prefixes every output line with the
    run UUID; attribution is exact even under fully overlapping concurrent runs.
  - **B-mode crons:** best-effort — output is windowed from the cron-daemon `CMD` syslog line
    to the next `CMD` line (or a fixed timeout cap). The `overlapping` flag is best-effort: the
    next-`CMD` windowing rule structurally assumes non-overlap, so it primarily catches the
    timeout-closed case, not a slow run that genuinely overlapped its successor (see §14).
- Run-log text **stays in VictoriaLogs**; queried on demand. SQLite stores only run
  *metadata* (boundaries, exit code, duration, line/byte counts, content digest) — never the
  log text itself.
- A reusable internal `VictoriaLogsClient` module + a **narrow, run-specific** run-log API
  endpoint.
- A `CronRunReconciler` scheduler-registered background task (event-sourced enrichment).
- The wrapper rewrite: generic shared script (fingerprint as argument, not baked in),
  run-UUID generation, per-line UUID prefixing of captured output, explicit start/end boundary
  markers, `run_id` threaded onto the heartbeat `/start|/ok|/fail` calls, and a
  wrapper-format-version migration path for the one existing wrapped cron.
- Anomaly detection **v1** — rule-based heuristic flags computed from `cron_runs` metadata.
- UI: a "Recent runs" teaser panel on the cron detail page, a dedicated run-history list
  route, and a dedicated run-log viewer route.

### Out of scope (explicitly deferred)

| Deferred item | Target |
| --- | --- |
| Live-tail (SSE/streaming) of in-flight run output | EPIC-004 (STAGE-004-005) |
| Error-keyword scanning of run output | EPIC-004 (STAGE-004-002 family) |
| Drain-style log-content clustering / templating of run output | EPIC-004 (STAGE-004-002) |
| Generic `/api/logs` LogsQL passthrough proxy | EPIC-004 (STAGE-004-005) |
| Anomaly detection on log *content* (vs. metadata) | EPIC-004, backfilled to cron runs |

For in-flight runs this stage provides **manual refresh**, not live-tail. For aged-out logs
(run record present, VL data past retention) the viewer shows an explicit "logs expired"
notice — this is acceptable, not an error.

### EPIC-004 cross-epic requirements (recorded here, applied when those stages are authored)

When EPIC-004 is designed, the following MUST be explicit acceptance criteria:

- **STAGE-004-002 (Drain clustering)** and the error-keyword work must apply **to cron run
  logs**, not only to generic service logs — i.e. anomaly detection v2/v3 is backfilled onto
  the `cron_runs` history produced by this work.
- **STAGE-004-005 (logs explorer / live tail)** must explicitly include **live-tail of
  in-flight cron runs** as in-scope, and the generic `/api/logs` proxy must be built on top of
  the `VictoriaLogsClient` module introduced here.

---

## 3. Architecture Overview

### 3.1 One-line architecture

A new per-invocation `cron_runs` table is written **synchronously** by the heartbeat receiver
(A-mode) and the log-scrape ingest (B-mode); a background `CronRunReconciler` **asynchronously
enriches** each closed run with VictoriaLogs-derived fields (line/byte counts, content digest,
anomaly flags); run-log text is read live from VictoriaLogs through a reusable
`VictoriaLogsClient`.

### 3.2 Data flow

```
A-MODE (wrapper-installed cron) — synchronous facts:
  cron fires → wrapper generates RUN_ID (UUID)
    → logger --tag hmrun "HM_RUN_START fp=<fp> run=<RUN_ID>"  (journald boundary marker)
    → POST /api/hb/<fp>/start?run_id=<RUN_ID>
        → heartbeat receiver INSERTs cron_runs row (state=running)        [SYNC]
    → run real command; stdout+stderr each line PREFIXED with `HM_RUN=<RUN_ID> `
        and piped through `logger --tag hmrun` (stable identifier)
        AND the ORIGINAL (un-prefixed) output echoed to original stdout/stderr
        → journald (SYSLOG_IDENTIFIER=hmrun) → Vector transform parses the
          `HM_RUN=<uuid>` prefix into a regular `run_id` field, strips it from
          the message body → VictoriaLogs
    → logger --tag hmrun "HM_RUN_END fp=<fp> run=<RUN_ID> exit=<n> duration=<s>"
    → POST /api/hb/<fp>/ok|fail?run_id=<RUN_ID>&duration=<s>&exit_code=<n>
        (exit_code is sent on both ok (0) and fail (non-zero))
        → heartbeat receiver UPDATEs cron_runs row (state, ended_at,
          duration_seconds, exit_code, vl_window_end)                      [SYNC]

B-MODE (unwrapped cron) — synchronous facts:
  cron daemon logs "(user) CMD (command)" → Vector → POST /api/internal/cron-events
    → the EXISTING cron-events ingest handler (`cron_events._process_one`),
      on the OBSERVED_RUN disposition, creates a cron_runs row IN ADDITION TO
      its existing record_observed_run behavior (run_id generated server-side,
      source=logscrape, state=running, started_at from CMD line ts)        [SYNC]
    → a later "exit=N" line (STATE_OK / STATE_FAIL disposition) closes the
      most-recent open run for that fingerprint within the time window     [SYNC]
    (a missing exit= line — or one dropped by the at-most-once cursor gap —
     leaves the run for the reconciler to window-finalize as state=unknown)

BOTH MODES — asynchronous enrichment:
  CronRunReconciler (scheduler-registered, ~30s tick):
    1. window-finalize  — close B-mode runs (next-CMD or timeout); set `overlapping`
    2. enrich           — for each run state!=running AND enriched_at IS NULL AND
                          ended >15s ago: VictoriaLogsClient query → line_count,
                          byte_count, content_digest → anomaly evaluator → anomaly_flags
                          → set enriched_at
    3. prune            — delete rows beyond retention (30 days / 50k-per-cron hard cap)

VIEWING:
  UI → GET /api/crons/<fp>/runs              → run-history list (SQLite only, session auth)
  UI → GET /api/crons/<fp>/runs/<run_id>/log → monitor builds LogsQL from the run's stored
        vl_window_* + run_id/fingerprint, queries VL server-side via VictoriaLogsClient,
        returns that run's lines (session auth)
```

### 3.3 New components

| Component | Location (indicative) | Purpose |
| --- | --- | --- |
| `cron_runs` table + migration | `apps/monitor/alembic/versions/0015_cron_runs.py` | Per-invocation run history. |
| `CronRunRepository` | `kernel/cron/run_repository.py` | CRUD over `cron_runs`. |
| `VictoriaLogsClient` | `kernel/logs/victorialogs_client.py` | Reusable bounded VL query module. |
| `CronRunReconciler` | `kernel/metrics/cron_run_reconciler.py` (or `kernel/cron/`) | Scheduler-registered enrichment/finalize/prune task. |
| Anomaly evaluator | `kernel/cron/run_anomaly.py` | Rule-based heuristic flag computation. |
| Run-history + run-log API | `kernel/api/routers/crons.py` (extend) | `GET /runs`, `GET /runs/{id}/log`. |
| Cron-events ingest (extended) | `apps/monitor/homelab_monitor/kernel/api/routers/cron_events.py` (**MODIFIED**) | B-mode `cron_runs` row create/close as an extension of the existing `_process_one` ingest handler. |
| Vector `hmrun` transform branch | `deploy/vector/vector.toml.template` (**MODIFIED/EXTENDED**) | New transform branch: parses lines with `SYSLOG_IDENTIFIER == "hmrun"`, extracts the `HM_RUN=<uuid>` prefix into a regular `run_id` field, strips the prefix from the message body. |
| Wrapper template (rewritten) | `apps/monitor/homelab_monitor/data/cron-with-heartbeat.sh.tmpl` | Generic, per-line UUID prefixing, boundary markers. |
| Wrapper env file | host `/etc/homelab-monitor/wrapper.env` | Per-deployment `HEARTBEAT_URL_BASE`. |
| UI run views | `apps/ui/src/components/crons/` + routes | Teaser panel, list route, log-viewer route. |

### 3.4 Relationship to existing `heartbeats_state`

`heartbeats_state` is **unchanged**. It remains the derived last-run/aggregate view that the
`HeartbeatStateCollector` (STAGE-002-010) reads to emit vmalert metrics. `cron_runs` is a new,
**additive** per-run history table. No existing `heartbeats_state` data is migrated. For an
A-mode invocation, the `cron_runs` row and the `heartbeats_state` update share the same
`run_id`.

---

## 4. Data Model — `cron_runs` Table

New table; new alembic migration — `0015_cron_runs.py` (head at spec time is `0014`; confirm
the head and renumber if it has advanced by implementation time).

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `run_id` | TEXT | NOT NULL | **PRIMARY KEY.** UUID. A-mode: generated by the wrapper. B-mode: generated server-side by the log-scrape ingest at row creation. |
| `cron_fingerprint` | TEXT | NOT NULL | References `crons.fingerprint`. Indexed. |
| `source` | TEXT | NOT NULL | `wrapper` (A-mode, UUID-exact) or `logscrape` (B-mode, heuristic). |
| `state` | TEXT | NOT NULL | `running` / `ok` / `fail` / `unknown`. `unknown` = B-mode run closed by timeout with no exit-code evidence. |
| `started_at` | TEXT | NOT NULL | UTC ISO-8601. |
| `ended_at` | TEXT | NULL | UTC ISO-8601. NULL while `state=running`. |
| `duration_seconds` | REAL | NULL | Set at close. |
| `exit_code` | INTEGER | NULL | A-mode: from the `/ok` (exit 0) or `/fail` call — the wrapper sends `exit_code` on both. B-mode: from the `exit=` syslog tag if present, else NULL. |
| `vl_window_start` | TEXT | NULL | UTC ISO-8601 — lower time bound for the VL log query. |
| `vl_window_end` | TEXT | NULL | UTC ISO-8601 — upper time bound. NULL while running. |
| `overlapping` | INTEGER | NOT NULL DEFAULT 0 | B-mode only: `1` if this run's window intersects another run of the same cron. |
| `enriched_at` | TEXT | NULL | UTC ISO-8601 — set when the reconciler completes VL enrichment. NULL = not yet enriched. |
| `line_count` | INTEGER | NULL | Output lines in the VL window. Set by reconciler. |
| `byte_count` | INTEGER | NULL | Output bytes. Set by reconciler. |
| `content_digest` | TEXT | NULL | Hash of *normalized* output (timestamps/PIDs stripped) — reflects content *shape*. **Computed in v1 but not consumed by any v1 anomaly rule** — a forward investment for EPIC-004 content-clustering / content-anomaly work (see §7). Set by reconciler. |
| `anomaly_flags` | TEXT | NOT NULL DEFAULT '' | Comma-separated heuristic flags (e.g. `duration_outlier,unexpected_empty`). Empty string = no anomaly. |

### Indexes

- `(cron_fingerprint, started_at DESC)` — serves the run-history list query.
- Partial index on `enriched_at IS NULL AND state != 'running'` — serves the reconciler's
  enrich work queue.
- `(cron_fingerprint, state)` — serves the reconciler's **window-finalize open-run scan**,
  which queries B-mode `state='running'` rows by fingerprint. The partial index above
  excludes `state='running'`, so a dedicated index is needed for this scan.

### Retention

A prune step (inside the reconciler tick) keeps run rows bounded by **both**:

- a time window — runs within the last **30 days** (env `HM_CRON_RUN_RETENTION_DAYS`, default 30); AND
- an absolute hard cap — at most **50,000 rows per cron** (env `HM_CRON_RUN_MAX_ROWS_PER_CRON`,
  default 50000).

Whichever bound is hit first prunes the oldest rows. The hard cap is a non-negotiable safety
valve: a per-minute cron generates ~43k rows/month, so the cap protects SQLite from unbounded
growth from a runaway high-frequency cron.

### `run_id` on heartbeat events

The heartbeat receiver's `/start`, `/ok`, `/fail` handlers gain an **optional** `run_id` query
parameter. The optional `run_id` field is added to **all three heartbeat query schema
classes** — `HeartbeatStartQuery`, `HeartbeatOkQuery`, and `HeartbeatFailQuery` (in
`apps/monitor/homelab_monitor/kernel/heartbeat/schemas.py`) — and `openapi.json` regenerates
as a result.

`HeartbeatOkQuery` gains both `run_id` and `exit_code` optional params; `HeartbeatStartQuery`
and `HeartbeatFailQuery` gain `run_id` (`HeartbeatFailQuery` already carries exit info).

- **Present** (A-mode, upgraded wrapper) → used to INSERT/UPDATE the matching `cron_runs` row.
- **Absent** (legacy wrapper, or the `/register` path) → behavior is **exactly as today** — no
  `cron_runs` row is created from the heartbeat path. This keeps legacy wrappers fully working
  until their one-time re-install.

**UPSERT semantics (`run_id` is the `cron_runs` PRIMARY KEY).** The heartbeat POSTs are
best-effort and lossy (`curl --max-time 5 ... || true`), so a lost `/start` is a NORMAL,
expected case — `/ok`/`/fail` must still produce a closed `cron_runs` row even with no prior
`/start`:

- **`/start` with a `run_id`** → INSERT-or-ignore. A duplicate `run_id` on `/start` (e.g. a
  wrapper retry or replay) is an idempotent no-op, not an error.
- **`/ok` and `/fail` with a `run_id`** → UPSERT (INSERT-or-UPDATE). If a prior `/start` row
  exists it is closed (`state`, `ended_at`, `duration_seconds`, `exit_code`,
  `vl_window_end`); if no prior row exists the UPSERT inserts a closed row directly. When
  inserting with no prior start, `started_at` is best-effort — derived from
  `ended_at - duration_seconds`.

---

## 5. Wrapper Rewrite (A-Mode Capture)

The wrapper template `apps/monitor/homelab_monitor/data/cron-with-heartbeat.sh.tmpl` is
reworked from a per-cron-baked script into a **generic, argument-driven** script.

### 5.1 Calling-convention change

| | Today | New |
| --- | --- | --- |
| Crontab line | `cron-with-heartbeat.sh -- <command>` | `cron-with-heartbeat.sh <fingerprint> -- <command>` |
| Fingerprint | baked into the script body as `FINGERPRINT='...'` | passed as argument `$1` |
| URL base | baked into the script body | read at runtime from `/etc/homelab-monitor/wrapper.env` |
| Shared script `/usr/local/bin/cron-with-heartbeat.sh` | rewritten per install (fingerprint differs) | **byte-identical for every cron** |

Because the shared script becomes byte-identical for every cron, **all future wrapper-logic
upgrades are a single shared-file replacement** — no per-cron re-install, no crontab edits.

The one genuinely per-deployment value — the heartbeat URL base — moves into a small config
file `/etc/homelab-monitor/wrapper.env` (alongside the existing `heartbeat.token`), containing
`HEARTBEAT_URL_BASE=...`. Written once by the install / host-setup path. The token file path
remains a fixed constant as today.

### 5.2 New wrapper behavior (per run)

1. Generate `RUN_ID` — `cat /proc/sys/kernel/random/uuid` (always present on Linux);
   `uuidgen` as fallback.
2. `logger --tag hmrun` a start boundary marker: `HM_RUN_START fp=<fp> run=<RUN_ID>`. The
   syslog identifier is the **stable constant `hmrun`** — never per-run.
3. `POST /api/hb/<fp>/start?run_id=<RUN_ID>`.
4. Run the real command. The captured copy of **stdout + stderr** has **each line prefixed
   with the stable parseable token `HM_RUN=<RUN_ID> `**, and that prefixed copy is piped
   through **`logger --tag hmrun`** (a STABLE identifier, not per-run). The **original,
   un-prefixed** output is still echoed to the original stdout/stderr (so a human running the
   cron by hand, or an existing logfile redirect, still sees clean output). In journald every
   captured line therefore has `SYSLOG_IDENTIFIER=hmrun` and an `HM_RUN=<uuid>` line prefix.
   A new Vector transform branch (§3.3) parses those lines, extracts `HM_RUN=<uuid>` into a
   regular VictoriaLogs `run_id` field, and strips the prefix from the message body.
5. Capture the real command's exit code and the run duration.
6. `logger` an end boundary marker: `HM_RUN_END fp=<fp> run=<RUN_ID> exit=<n> duration=<s>`.
7. `POST /api/hb/<fp>/ok|fail?run_id=<RUN_ID>&duration=<s>&exit_code=<n>` — `exit_code` is
   sent on both the `/ok` call (`0`) and the `/fail` call (the non-zero code).

Heartbeat POSTs remain best-effort and bounded (`curl --max-time 5`, all output discarded,
`|| true`) — a network failure NEVER blocks or alters the real command. This principle is
unchanged from the current wrapper.

### 5.3 Wrapper edge cases (mandatory)

- **Exit-code preservation.** The original command's exit code is *always* preserved and
  returned as the wrapper's exit code. The per-line-prefix + `logger` pipe must NOT mask it —
  use a pipe-status-safe construct (capture the command's status explicitly, not the
  pipeline's).
- **`logger` missing or failing.** If `logger` is unavailable or errors, output still goes to
  the original stdout/stderr and the run still completes — capture degrades gracefully, the
  cron never breaks.
- **UUID generation failure.** Extremely unlikely on Linux; if both UUID sources fail the
  wrapper still runs the command (capture degrades).

### 5.4 Wrapper-format migration

A `WRAPPER_FORMAT_VERSION` marker is introduced:

- A version constant embedded in the wrapper template, and recorded in the `crons` record at
  install time via an **explicit new `wrapper_format_version` column** on the `crons` table
  (queryable and testable — no reliance on derived wrapper-health state for the stored value).
  The column is added by its own alembic migration — `0016_crons_wrapper_format_version.py`
  (decided in STAGE-002-012; confirm the head at implementation time).
- The outdated-format state is surfaced by **extending the `WrapperHealth` Literal type**
  (`apps/monitor/homelab_monitor/kernel/cron/schemas.py`, currently
  `Literal["ok", "stale", "unknown"]`) with a new member **`"format_outdated"`**.
- A cron whose installed wrapper predates the run-log format is surfaced via the **existing
  wrapper-health badge** (STAGE-002-010) rendering the new `"format_outdated"` state:
  **"wrapper format outdated — re-install to enable run logs"**.
- The user re-installs per cron via the existing one-click install UI. **No silent crontab
  edits** — consistent with the EPIC-002 "no cron modified without explicit dashboard
  confirmation" policy. (Only one wrapped cron exists on the prod host today, so this is a
  trivial one-time action.)
- After re-install, that cron's crontab line uses the new `<fingerprint> --` convention, and
  every subsequent wrapper-logic upgrade is a free shared-file swap.

### 5.5 Install-executor / `hm-cron-apply.sh` changes

- `_rewrite_line` emits the new `<fingerprint> --` prefix shape.
- `_build_wrapper_content` stops substituting `{{FINGERPRINT}}` and `{{HEARTBEAT_URL_BASE}}`
  into the script body (fingerprint becomes an argument; URL base becomes a `wrapper.env`
  value).
- A new operation writes `/etc/homelab-monitor/wrapper.env`.
- The unwrap logic (STAGE-002-009A) is updated to strip the new prefix shape.
- STAGE-002-009A's decisions hold: the shared wrapper script is never removed on unwrap; the
  shared token file is never touched.

---

## 6. VictoriaLogsClient & CronRunReconciler

### 6.1 `VictoriaLogsClient`

A new reusable kernel module — the genuinely reusable piece; EPIC-004's generic `/api/logs`
proxy will later be built on top of it.

**Responsibilities:**
- Connect to VictoriaLogs (`http://victorialogs:9428` in prod; configurable; dev publishes
  `19428` per the project port map).
- Execute a LogsQL query over a bounded time window.
- Parse the JSON-lines response into structured log lines (timestamp, message, journald
  fields).

**Hard limits (mandatory — not optional):**
- Every query is bounded by an explicit time range.
- A max-lines cap (`HM_VL_QUERY_MAX_LINES`, default 10000).
- A max-bytes cap — env `HM_VL_QUERY_MAX_BYTES`, default 5000000 (5 MB).
- An HTTP timeout.
- A run that produced more than the cap returns the capped lines plus a `truncated: true`
  flag — never an unbounded fetch.

**Query shapes this work needs:**
- **A-mode:** `SYSLOG_IDENTIFIER:hmrun AND run_id:<run_id>` over
  `[vl_window_start, vl_window_end]` — UUID-exact, correct even under fully overlapping
  concurrent runs. `run_id` is a regular VictoriaLogs field (not a stream field), so stream
  cardinality stays low; the time-window bound keeps the regular-field filter performant.
- **B-mode:** the cron-fingerprint heuristic (reuse STAGE-002-008's fingerprint-match logic)
  over the run window.

**Failure handling:** VL unreachable / timeout / non-200 → raise a typed error. Callers
(reconciler, log endpoint) degrade gracefully and never crash.

### 6.2 `CronRunReconciler`

A scheduler-registered background task that mirrors the `HeartbeatStateCollector` pattern from
STAGE-002-010 (same registration shape in `lifespan.py`, same scheduler). Runs every ~30s
(env-configurable). Each tick performs three phases:

1. **Window-finalize.** For B-mode runs still `state=running`: if a newer `CMD` line exists
   for the same cron, close the prior run (`ended_at` = the next run's `started_at`;
   `state=unknown` unless an `exit=` tag was observed for it, else `ok`/`fail` from that tag);
   if a run has been open past the fixed max-duration cap, close it by timeout
   (`state=unknown`). Set `overlapping=1` on any B-mode runs whose windows intersect another
   run of the same cron. **Note:** because the next-`CMD` rule closes a run *at* the next
   run's start, it structurally assumes non-overlap — so `overlapping=1` primarily catches the
   *timeout-closed* case (a run whose timeout window extends past a later run's start). A slow
   run that genuinely overlapped its successor cannot be reliably detected under this rule
   (see §14). **A-mode runs are closed synchronously by the `/ok|/fail` call — the reconciler
   does not change A-mode run state.**

2. **Enrich.** For each run where `state != 'running' AND enriched_at IS NULL AND ended_at is
   older than a short grace delay (~15s, to let VL ingest trailing lines)`: query
   `VictoriaLogsClient` for the run's window, compute `line_count`, `byte_count`, and
   `content_digest` (a hash of *normalized* output — strip timestamps/PIDs so the digest
   reflects content shape, enabling anomaly comparison), run the anomaly evaluator (§7), write
   `anomaly_flags`, and set `enriched_at`.

3. **Prune.** Delete `cron_runs` rows beyond the retention bound (30 days / 50k-per-cron hard
   cap, §4).

### 6.3 B-mode event correlation

B-mode `cron_runs` row creation/closing is **an extension of the existing
`cron_events._process_one` ingest handler**
(`apps/monitor/homelab_monitor/kernel/api/routers/cron_events.py`), operating on the handler's
existing `OBSERVED_RUN` / `STATE_OK` / `STATE_FAIL` dispositions and reusing the existing
`match_by_log_key` fingerprint resolution. It is **NOT a new endpoint and NOT a parallel
ingest path**. A single B-mode run is observed as **two independent events** — the `CMD` start
line and a later `exit=N` line if present — each with its own `journal_cursor`.

- **`CMD` start line** (an `OBSERVED_RUN` disposition) → creates a `cron_runs` row:
  server-generated `run_id` UUID, `source=logscrape`, `state=running`, `started_at` from the
  event timestamp.
- **`exit=N` line** (a `STATE_OK` / `STATE_FAIL` disposition) → correlated to *the most recent
  open (`state=running`) `cron_runs` row for that fingerprint whose window contains the exit
  line's timestamp*, and closes it (sets `state`, `ended_at`, `exit_code`,
  `duration_seconds`). This correlation is heuristic — most-recent-open-run-by-fingerprint
  within the time window.
- **Missing `exit=` line** — if the at-most-once cursor gap drops the `exit=` event, or the
  cron emits no `exit=` line at all, the run is later closed by the reconciler's
  window-finalize with `state=unknown` (see D8).
- **Row-creation idempotency** is provided by the existing `try_claim_cursor` cursor claim: a
  replayed event is rejected *before* row creation — NOT by the reconciler.
- Creating a `cron_runs` row happens **IN ADDITION TO** the existing `record_observed_run` /
  state-write behavior of the handler — it does not replace it.

**Properties:**
- **Reconciler idempotency** — the reconciler is idempotent and stateless: a missed or re-run
  tick simply re-derives windows and re-runs enrichment. This is distinct from **B-mode
  row-creation idempotency**, which is provided by the existing cron-events cursor claim
  (`try_claim_cursor`) — a replayed event is rejected before any `cron_runs` row is created
  (see §6.3). The reconciler does NOT provide row-creation idempotency.
- If VL is down: enrichment for that tick is skipped (rows stay `enriched_at IS NULL`, retried
  next tick); window-finalize and prune still proceed (they do not need VL).
- Emits scheduler-task run self-metrics — the same self-observation pattern as
  `HeartbeatStateCollector` (project mandate: "plugins observe themselves" applies to all
  scheduler-registered tasks).

---

## 7. Anomaly Detection v1 (Heuristic)

The anomaly evaluator is invoked by the reconciler during the **enrich** phase. It is
rule-based, computed entirely from `cron_runs` history for the same cron — no log-content
analysis, no ML. Every rule that trips appends a flag to the run's `anomaly_flags`.

| Flag | Rule |
| --- | --- |
| `duration_outlier` | `duration_seconds` exceeds `k ×` the rolling p95 of the last `N` completed runs of this cron (`k`, `N` env-configurable). |
| `exit_code_changed` | `exit_code` differs from this cron's recent dominant exit code. |
| `output_size_spike` | `line_count` (or `byte_count`) exceeds the rolling-median band for this cron. |
| `output_size_drop` | `line_count` (or `byte_count`) falls below the rolling-median band for this cron. |
| `unexpected_empty` | `line_count == 0` for a cron that normally produces output. |
| `new_failure` | `state == fail` when this cron's recent runs were all `ok`. |

**Min-history gate.** Every rule needs baseline history to be meaningful. A rule does not fire
until at least `min_history` completed runs exist for the cron (env-configurable). This means
anomaly flags begin producing signal only after the `cron_runs` table has accumulated history
— which is expected and correct.

**Explainability.** All flags are rule-derived and human-explainable (e.g. "this run took 4×
its p95 duration"). No black-box scoring.

**`content_digest` is not consumed by any v1 rule.** The reconciler still *computes*
`content_digest` (a hash of normalized output), but no v1 anomaly rule in the table above uses
it. It is a deliberate forward investment: capturing historical digests now means EPIC-004's
content-clustering / content-anomaly work has a comparison baseline to work against. The exact
output-normalization regex — which timestamp / PID formats are stripped before hashing — is a
STAGE-002-014 implementation detail, deliberately not pinned in this spec.

EPIC-004 layers error-keyword scanning and Drain-style content clustering on top later (see
§2 cross-epic requirements).

---

## 8. API

All run-history / run-log **read** endpoints sit behind the **normal session auth**, alongside
the existing `GET /api/crons/{fingerprint}`. The wrapper's `/start|/ok|/fail` calls (now
carrying the optional `run_id`) stay on the existing `heartbeat:write` API-token path. This
split is consistent with the EPIC-002 auth boundary (ingestion = token; dashboard reads =
session).

### 8.1 `GET /api/crons/{fingerprint}/runs`

Run-history list. Pure SQLite read — never touches VL.

- **Query params:** `limit` (default 50, capped), `cursor` (pagination), optional `state`
  filter.
- **Returns:** a list of run records — `run_id`, `state`, `started_at`, `ended_at`,
  `duration_seconds`, `exit_code`, `source`, `overlapping`, `line_count`, `byte_count`,
  `anomaly_flags`, and an `enriched` boolean. **No log text.**

### 8.2 `GET /api/crons/{fingerprint}/runs/{run_id}/log`

The narrow run-log endpoint. The monitor loads the run record, builds the LogsQL query from
the stored `vl_window_*` plus the run identity (A-mode:
`SYSLOG_IDENTIFIER:hmrun AND run_id:<run_id>`; B-mode: fingerprint heuristic), calls
`VictoriaLogsClient` server-side, and returns that run's lines. The frontend never sees
LogsQL and never talks to VL directly.

For a run still `state=running` — which has `vl_window_end = NULL` — the endpoint substitutes
`now()` as the query's upper time bound, so the output produced so far is returned.

Three response shapes:

| Situation | Response |
| --- | --- |
| Run completed, VL data present | `200` — log lines + `truncated` flag + `log_status: available`. |
| Run still `running` | `200` — output-so-far + `state: running` (UI shows a manual refresh button). |
| VL data aged out (window older than VL retention) | `200` — `log_status: expired` + the run metadata, no log text. **Not an error.** |
| VL unreachable / timeout | `503` — typed error; UI renders "logs temporarily unavailable". |

The narrow endpoint is purpose-built and fully testable. The reusable VL-client plumbing
underneath it is what EPIC-004's generic proxy will reuse — the generic `/api/logs` proxy
itself stays out of scope here.

---

## 9. UI

Three surfaces, following the existing Inventory → detail structure.

### 9.1 "Recent runs" teaser panel on `CronDetail.tsx`

A new (5th) panel on the cron detail page showing the **last 3–5 runs** — timestamp, duration,
an exit-code chip, and an anomaly badge when the run has `anomaly_flags`. Includes a
"View all runs →" link to the dedicated list route. Keeps the detail page light; the full
history lives on its own page.

### 9.2 `/crons/{fingerprint}/runs` — run-history list route

A dedicated full-page route: a paginated run-history table (time, duration, exit-code chip,
`source`, anomaly badges, an `overlapping` indicator for B-mode runs), with a filter by state.
Each row links to the log viewer. Deep-linkable.

### 9.3 `/crons/{fingerprint}/runs/{run_id}` — run-log viewer route

A dedicated route: a run-metadata header (state, duration, exit code, anomaly flags) above the
log text (monospace, scrollable). Behaviors:

- In-flight run → output-so-far + a **manual refresh** button (no live-tail this stage).
- Aged-out logs → an explicit "log text no longer available (past VictoriaLogs retention)"
  notice plus the run metadata.
- VL unreachable → a "logs temporarily unavailable" message (not a hard error page).

Deep-linkable so a specific failed run can be bookmarked/shared.

---

## 10. Error Handling Principles

- **The cron never breaks.** Every wrapper addition (UUID generation, the `logger` pipe,
  boundary markers, heartbeat POSTs) is best-effort. The original command's exit code is
  always preserved and returned. If `logger` is missing or the monitor/VL is unreachable,
  capture degrades — the cron runs unaffected.
- **VL unavailability is graceful everywhere.** Reconciler: skip enrichment this tick, retry
  next. Log endpoint: `503` + typed error → UI "temporarily unavailable". The run-history list
  never depends on VL (pure SQLite).
- **Partial / missing data is explicit, never silent.** Aged-out logs → "expired" state.
  B-mode overlap → `overlapping` flag + a UI caveat. B-mode timeout-closed run →
  `state=unknown`. No fabricated data.
- **The reconciler is idempotent and stateless** — a crash or missed tick re-derives on the
  next run.

---

## 11. Testing Strategy

### Backend (100% kernel coverage gate, per project standard)

- `cron_runs` repository CRUD.
- The new alembic migration — round-trip (up/down) test.
- Heartbeat receiver `run_id` threading — both present and absent.
- `run_id` idempotency / lost-start UPSERT — duplicate `/start` is a no-op; `/ok`/`/fail` with
  no prior `/start` still produces a closed row (`started_at` derived).
- B-mode log-scrape ingest — row create/close, `exit=` event correlation to the most-recent
  open run, overlap detection, timeout close.
- `VictoriaLogsClient` against a mocked VL — success, truncation, timeout, non-200.
- `CronRunReconciler` — window-finalize, enrich, prune (each phase independently).
- Anomaly evaluator — each rule, including the min-history no-fire case.
- Both API endpoints — running / expired / unreachable / normal response shapes.
- Retention prune — both the 30-day window and the 50k-per-cron hard cap.

### Frontend (vitest)

- The "Recent runs" teaser panel.
- The run-history list route.
- The run-log viewer — in-flight, expired, unavailable, and normal states.

### Integration / prod rig

Run-log capture is a host-integration feature (it touches the real wrapper, the real host
crontab, journald, Vector, and VictoriaLogs). Refinement therefore does **both** sub-phases:

- **3a (dev rig)** — synthetic / fake data against `make dev`.
- **3b (prod rig)** — the real upgraded wrapper on the prod host: install the upgraded wrapper,
  run a cron, confirm a `cron_runs` row is written, confirm UUID-tagged lines reach
  VictoriaLogs, confirm the run-log endpoint returns them. Validate the wrapper-format
  migration path — the one existing prod wrapped cron re-installs cleanly onto the new
  `<fingerprint> --` convention.

---

## 12. Stage Decomposition

EPIC-002 is **re-opened**: status flips from `Complete` back to `In Progress`; Current Stage
→ STAGE-002-011. Five appended stages. The stages are **ordered** and each builds on prior
stages; the recommended build order is 011 → 012 → 013 → 014 → 015. One exception to a strict
dependency chain: STAGE-002-014's anomaly evaluator can be developed and unit-tested against
synthetic `cron_runs` history independently of STAGE-002-013 (it needs only STAGE-002-011's
table).

| Stage | Scope | Host-integration? |
| --- | --- | --- |
| **STAGE-002-011** | `cron_runs` table + alembic migration + `CronRunRepository`; heartbeat receiver accepts and threads the optional `run_id` (added to `HeartbeatStartQuery` / `HeartbeatOkQuery` / `HeartbeatFailQuery`, with `openapi.json` regeneration); synchronous A-mode `cron_runs` row create/close from the heartbeat path with INSERT-or-ignore / UPSERT semantics. Backend foundation, no host changes. | No (3a only) |
| **STAGE-002-012** | Wrapper rewrite — generic shared script, fingerprint-as-argument, `/etc/homelab-monitor/wrapper.env`, run-UUID generation, per-line `HM_RUN=<uuid>` output prefixing, start/end boundary markers, `run_id` threaded onto the heartbeat calls; the new `deploy/vector/vector.toml.template` transform branch that parses `SYSLOG_IDENTIFIER == "hmrun"` lines and extracts the `run_id` field; `hm-cron-apply.sh` / install-executor changes; `WRAPPER_FORMAT_VERSION` — adding the `wrapper_format_version` column to `crons` (its own migration `0016_crons_wrapper_format_version.py`), extending the `WrapperHealth` Literal with `"format_outdated"`, the consequent `openapi.json` regeneration, and the UI badge update rendering the new state. | **Yes (3a + 3b)** |
| **STAGE-002-013** | `VictoriaLogsClient` (bounded VL query module) + `CronRunReconciler` (window-finalize, enrich, prune) + B-mode `cron_runs` row create/close as an extension of the existing `cron_events._process_one` ingest handler (`exit=` event correlation, the at-most-once contract) + the `overlapping` flag + the A-mode `SYSLOG_IDENTIFIER:hmrun AND run_id:<run_id>` VL query consumption. | Yes (3a + 3b) |
| **STAGE-002-014** | Run-history API (`GET /runs`) + the narrow run-log endpoint (`GET /runs/{id}/log`) + the anomaly heuristic evaluator wired into the reconciler. | No (3a only) |
| **STAGE-002-015** | UI — the `CronDetail` "Recent runs" teaser panel, the `/crons/{fp}/runs` list route, and the `/crons/{fp}/runs/{run_id}` log-viewer route. | No (3a only; frontend) |

After STAGE-002-015 Finalize, EPIC-002 is complete again at 17 stages
(STAGE-002-001 … 010 plus 007A, 009A, and 011 … 015).

---

## 13. Locked Design Decisions

| ID | Decision |
| --- | --- |
| **D1** | Capture scope: **all discovered crons** get run history. A-mode crons get high-fidelity UUID-tagged output; B-mode crons get best-effort fingerprint-windowed output. |
| **D2** | Run-log **text stays in VictoriaLogs**, queried on demand. SQLite (`cron_runs`) stores only run metadata + a VL pointer + a content digest. |
| **D3** | A new `cron_runs` table is the per-invocation history. `heartbeats_state` is unchanged (it stays the derived last-run/aggregate view). |
| **D4** | Run retention is **time-based (30 days)** with an absolute **hard cap of 50,000 rows per cron**; whichever bound hits first prunes. Both env-configurable. |
| **D5** | The wrapper becomes a **generic shared script** — fingerprint passed as argument `$1`, URL base read from `/etc/homelab-monitor/wrapper.env`. All future wrapper-logic upgrades become a single shared-file replacement. |
| **D6** | The wrapper generates a **run UUID** per invocation. Captured output goes to `logger` under the **stable syslog identifier `hmrun`** (never per-run), with **each line prefixed by the parseable token `HM_RUN=<uuid>`**; a new Vector transform branch extracts that prefix into a **regular `run_id` field** (not a stream field — avoids stream-cardinality explosion). The wrapper emits `HM_RUN_START`/`HM_RUN_END` boundary markers and threads `run_id` onto the heartbeat `/start|/ok|/fail` calls. A-mode attribution is exact under overlap via `SYSLOG_IDENTIFIER:hmrun AND run_id:<uuid>`. |
| **D7** | Legacy wrappers are migrated via a `WRAPPER_FORMAT_VERSION` marker recorded in an explicit `wrapper_format_version` column on the `crons` table + the existing wrapper-health badge rendering a new `WrapperHealth` value `"format_outdated"` ("wrapper format outdated"); the user re-installs per cron via the existing one-click UI. **No silent crontab edits.** |
| **D8** | B-mode run-end boundary = the next `CMD` line for the same cron, OR a fixed max-duration timeout cap. Overlapping B-mode runs are flagged (`overlapping=1`), not silently mis-attributed. B-mode runs closed by timeout with no exit evidence get `state=unknown`. |
| **D9** | Run records are written **synchronously** by the heartbeat receiver (A-mode) and as an extension of the existing cron-events log-scrape ingest (B-mode); a background **`CronRunReconciler`** asynchronously enriches each closed run with VL-derived fields. (Event-sourced reconciler — Approach A.) The B-mode synchronous write is bounded by the existing ingest's **at-most-once** semantics — a dropped event means a missing run row — and the `exit=` event correlation is heuristic (most-recent-open-run-by-fingerprint within the time window). |
| **D10** | A reusable internal **`VictoriaLogsClient`** module is built now (with mandatory bounded-query limits); the **narrow run-specific** run-log endpoint is the only VL-backed API this work exposes. The generic `/api/logs` proxy stays in EPIC-004, built later on top of `VictoriaLogsClient`. |
| **D11** | Anomaly detection **v1 is rule-based heuristics** on `cron_runs` metadata only (duration outlier, exit-code change, output-size spike/drop, unexpected-empty, new-failure), gated by a min-history threshold. Error-keyword scanning and Drain content clustering are explicitly EPIC-004 and must be backfilled to cron logs there. |
| **D12** | UI = a "Recent runs" teaser panel on `CronDetail` + a dedicated `/crons/{fp}/runs` list route + a dedicated `/crons/{fp}/runs/{run_id}` log-viewer route (deep-linkable). In-flight runs get **manual refresh**, not live-tail. Aged-out logs show an explicit "expired" notice. |
| **D13** | Run-history / run-log **read** endpoints use the **normal session auth**. The wrapper's heartbeat `/start|/ok|/fail` calls (with `run_id`) stay on the `heartbeat:write` token path. |
| **D14** | This work **re-opens EPIC-002** as appended stages STAGE-002-011 … STAGE-002-015. It is NOT a new epic. |
| **D15** | EPIC-004's design must explicitly include cron run logs: STAGE-004-002 (Drain clustering / error-keyword) backfilled to cron run logs, and STAGE-004-005 (logs explorer) must include **live-tail of in-flight cron runs** and build the generic `/api/logs` proxy on `VictoriaLogsClient`. |

---

## 14. Open Questions / Risks

- **B-mode boundary fuzziness** — windowing unwrapped-cron output by `CMD`-line boundaries is
  inherently heuristic. Mitigated by the `overlapping` flag and an explicit UI caveat;
  acceptable because A-mode (the user's actively-monitored crons) is exact.
- **B-mode overlap detection is best-effort-timeout-only** — the next-`CMD` windowing rule
  closes a run *at* the next run's start, so it structurally assumes non-overlap. `overlapping`
  therefore reliably catches only the timeout-closed case; a slow B-mode run that genuinely
  overlapped its successor is not reliably detectable without a different signal. A-mode
  overlap is exact (per-line UUID).
- **B-mode dropped `CMD` line** — a `CMD` start event dropped by the existing ingest's
  at-most-once cursor gap means that run is silently missing from history (no `cron_runs`
  row). This is an accepted limitation, consistent with the existing cron-events ingest's
  at-most-once delivery contract — the run-log feature does not strengthen it.
- **VictoriaLogs `run_id` is a regular field, not a stream field** — chosen deliberately to
  avoid stream-cardinality explosion (one stream per run would be thousands of streams, which
  VictoriaLogs warns against). Per-run queries are bounded by the run's time window, so a
  regular-field `run_id` filter is performant enough at homelab scale.
- **VL ingestion lag** — the reconciler's ~15s enrich grace delay assumes Vector → VL
  ingestion completes within that window. If a deployment has slower ingestion, a run's last
  lines could be missed. Mitigation: the grace delay is env-configurable; the reconciler can
  be made to re-enrich if a later tick detects more lines (a v1.1 refinement, not required for
  first ship).
- **Wrapper exit-code preservation through a `logger` pipe** — must use a pipe-status-safe
  shell construct. This is a known shell footgun and a specific test target in STAGE-002-012.
- **Migration head number** — the `cron_runs` migration must chain from the current head
  (`0014` at spec time); confirm at implementation.
