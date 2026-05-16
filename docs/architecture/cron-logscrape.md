# Cron log-scrape pipeline (B-mode evidence collection)

> Last updated: 2026-05-15 (STAGE-002-008 — Vector push Option D).

## TL;DR

The monitor observes cron activity on the local host via structured-log parsing
(not polling). Vector parses cron journald entries into JSON events and pushes
them to `POST /api/internal/cron-events`. The endpoint matches each event to a
cron row by `(host, log_match_key)`, then records a NEUTRAL "observed run" for
bare cron dispatch lines or a real `ok`/`fail` state transition for
wrapper-tagged `exit=N` lines. Idempotency is keyed on the journald `__CURSOR`.

---

## Why Option D (push) not poll

VictoriaLogs (the logs backend) has no stable record-ID or cursor API. Polling
and deduping against VL's storage would require:

1. Querying the logs API repeatedly
2. Deduping by log content (fragile — timestamps, UIDs, JSON formatting change)
3. Handling VL retention/compaction that may delete old logs

Vector's journald source solves this differently: it checkpoints the journald
`__CURSOR` locally (in a Vector state file) and only advances after the
*downstream app* acknowledges. This is the standard at-least-once + idempotent-write
pattern.

Vector STILL ships raw logs to VictoriaLogs in parallel (for the logs UI). This
is a separate pipeline that does not interfere with structured event ingest.

---

## Pipeline

```
journald
  ↓ (filter: cron.service, crond.service)
cron_journald_filter
  ↓ (VRL: parse MESSAGE, extract user/command/exit_code, attach __CURSOR)
cron_parsed
  ↓ (filter: .cron_parse_ok == true)
cron_parsed_ok
  ↓ (remap to CronEventItem shape)
cron_event_shape
  ↓ (HTTP sink with bearer auth, disk buffer, acks)
POST /api/internal/cron-events
  ↓
match_by_log_key(host, log_match_key)
  ↓ (found 0 matches → drop; 1 match → continue; 2+ → skip, increment ambiguous counter)
cron row
  ↓ (claim cursor in cron_log_cursors, then write state)
observed_runs_total++ and/or current_state ← ok/fail
```

**Key assumption:** the `journald` source is wired by default and is the only
source in the committed config. Non-systemd hosts use the syslog fallback
(Section "journald default / syslog fallback" below).

---

## What log evidence asserts (D1 — what a line proves)

A vanilla `(root) CMD (/storage/scripts/backup.sh)` cron log line has NO exit
code. It proves that cron *fired* the job and systemd logged the fact — it does
NOT prove that the job succeeded or finished.

| Log line form                                 | Disposition               | `current_state` change | Metrics             |
|-----------------------------------------------|---------------------------|----------------------|---------------------|
| `(user) CMD (command)` (vanilla, no `exit=`) | Neutral observed run       | **UNCHANGED**          | `observed_runs_total++` |
| `(user) CMD (command exit=0)` (wrapper-tagged) | OK state transition        | `unknown`→`ok`        | (state logic) |
| `(user) CMD (command exit=N)` (N≠0)            | FAIL state transition      | `unknown`/`ok`→`fail` | (state logic) |

The distinction is critical: the MONITOR cannot infer job success from the log.
Only a wrapper-instrumented cron (one with the `exit=N` tag in the command
string) can assert outcome.

---

## Command matching (D5 — matching disk to log)

Disk-side cron discovery (STAGE-002-007) stores each command in the `crons` row.
Log-side Vector parses the `MESSAGE` field from journald. For the two to match,
both must compute the same key.

The key is `canonical_log_key(command)`:

```python
def canonical_log_key(command: str) -> str:
    # 1. Scrub secrets (API keys, passwords, etc.)
    scrubbed = scrub_secrets(command)

    # 2. Collapse whitespace: "cmd  arg" → "cmd arg"
    normalized = " ".join(scrubbed.split())

    # 3. Strip one layer of `(...)` if present
    # (some discovery sources wrap the command)
    stripped = normalized.removeprefix("(").removesuffix(")")

    return stripped
```

**Worked example:**

Disk command: `mysqldump -u root -psecret db`
Log command (from MESSAGE): `mysqldump -u root -psecret db`

Disk side computes: `canonical_log_key("mysqldump -u root -psecret db")`
  → scrub → `"mysqldump -u root -p<redacted> db"`
  → normalize → same
  → strip → same
  → result: `"mysqldump -u root -p<redacted> db"`

Log side computes: same
  → result: `"mysqldump -u root -p<redacted> db"`

Match! The cron row's `observed_runs_total` increments.

The join is on `(host, log_match_key)`.

**Empty canonical key → NULL.** If `canonical_log_key` returns an empty string (e.g., a command that is blank after scrubbing and stripping), the repository stores `crons.log_match_key = NULL` rather than `""`. This is handled by `_log_match_key_or_none` in `CronRepo`. A NULL `log_match_key` never matches any log event (SQL `= :key` with a non-NULL key cannot match NULL), so such crons are effectively invisible to the log-scrape pipeline and remain in the `unknown` state.

---

## The `%`-substitution limitation

Cron treats an unescaped `%` in a crontab line specially: text after the FIRST
`%` becomes the command's stdin. For example:

```
30 2 * * * /backup.sh % --verbose
```

On disk, the command appears as `/backup.sh`. In the journal, cron logs what it
actually ran: `/backup.sh` (the `% --verbose` part was consumed as stdin).

`canonical_log_key` cannot reconcile this; the scrubbed, normalized versions
differ. Crons using `%` will not match log evidence and will remain in the
`unknown` state. This is a documented accepted limitation.

Workaround: Wrap the cron (use `hm cron install-wrapper`) to explicitly add the
`exit=N` tag, bypassing log matching entirely.

---

## Idempotency model (D3 — the cursor claim ledger)

Vector's `http` sink with `acknowledgements.enabled = true` (Vector's default
idempotency mode) only advances the journald checkpoint after the app
acknowledges the HTTP `POST`. The app writes a row to `cron_log_cursors` for
each event processed, using the `journal_cursor` as the primary key.

```sql
CREATE TABLE cron_log_cursors (
    journal_cursor TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
```

**Ingest flow:**

1. Vector parses a journald entry, POSTs the event to `/api/internal/cron-events`.
2. The endpoint reads the event's `journal_cursor`.
3. Inside a transaction:
   a. `INSERT OR IGNORE INTO cron_log_cursors (journal_cursor, processed_at) VALUES (?, ?)`.
   b. If `rowcount == 0`: this cursor was already processed → skip the state write,
      respond `202` (idempotent).
   c. If `rowcount == 1`: first sighting → continue to state matching and write.
4. Commit the transaction.
5. Return `202 Accepted` to Vector.
6. Vector acknowledges to journald, journald advances the checkpoint.

**Crash window (honest limitation):**

If the process dies between step 3c's commit and step 5's response, Vector retries
the same event. On the retry, the cursor already exists in step 3b, so the state
write is skipped and `202` is returned (idempotent). But if the process dies
between step 3c and step 4 (before the state commit), the state write is lost —
that single event's observed run or state transition is NOT recorded.

This is an accepted single-host, single-writer trade-off. In a production
multi-replica system, you would use a distributed transaction or sagas; for a
homelab monitor, the one-event loss on ungraceful shutdown is acceptable.

---

## journald default / syslog fallback (D2)

### journald (default, wired)

The committed `deploy/vector/vector.toml.template` uses the `journald` source, filtering
to entries with `._SYSTEMD_UNIT` = `cron.service` or `crond.service`. Each entry
includes a `__CURSOR` field — a stable, unique, per-entry identifier. Journald
guarantees that the cursor is:

- Unique per journal entry (no collisions)
- Stable across reads (same entry → same cursor every time)
- Sufficient for checkpointing (Vector remembers the cursor, resets to it on
  startup, and only advances after ack)

### syslog fallback (non-systemd hosts)

Hosts without systemd use `/var/log/cron` or `/var/log/syslog`. The file source
has no built-in cursor; we must synthesize one:

```python
def synthesize_cursor(event: CronLogEvent) -> str:
    # Hash of (host, timestamp, user, command)
    payload = json.dumps({
        "host": event.host,
        "timestamp": event.timestamp,
        "user": event.user,
        "command": event.command
    }, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()
```

**Weaker idempotency:** if the SAME cron command runs twice in the same
sub-second interval on the same host, the hashes collide and the second run is
mistaken for a replay. This is rare but possible.

**Configuration:** In `deploy/vector/vector.toml.template`, there is a commented-out
`[sources.cron_syslog]` block. To use it:

1. Uncomment the `[sources.cron_syslog]` block.
2. Remove or disable the `cron_journald_filter` transform (or create a
   conditional so both sources are NOT wired to the same downstream pipeline —
   both would cause double-ingest).
3. Update the pipeline to use `cron_syslog` as the input source instead of
   `journald`.

**NEVER enable both journald and syslog filters on the same Vector instance.**

---

## Multiple-match policy (D4)

If one log event matches 2+ cron rows (by `(host, log_match_key)`, excluding
soft-deleted rows), the endpoint:

1. Does NOT write any state change (could be a race; updating all would
   manufacture phantom evidence).
2. Increments `homelab_cron_logscrape_ambiguous_total{host}`.
3. Logs a structured warning with the candidate fingerprints.
4. Still claims the cursor (the event WAS processed — to a "skip" decision).

A replay of the same ambiguous event is also skipped (cursor already claimed).

---

## Metrics

| Metric                                      | Type    | Labels | Meaning |
|---------------------------------------------|---------|--------|---------|
| `homelab_cron_logscrape_matches_total`      | Counter | `host` | Total events matched to a single cron row and state was written. |
| `homelab_cron_logscrape_ambiguous_total`    | Counter | `host` | Total events that matched 2+ rows (state write skipped). |

---

## Auth

Vector authenticates with a dedicated `cron-events-ingest` API token. The token
is:

- Minted at monitor boot-up by `ensure_cron_events_token()` (in
  `kernel/cron/log_ingest_token.py`).
- Scoped to `Scope.CRON_EVENTS_INGEST_WRITE`.
- Stored plaintext in the secrets store under the key `cron-events-ingest-token`.

**Operator setup: none.** `docker compose up -d` is the whole procedure.

At boot the monitor renders `deploy/vector/vector.toml.template` to a shared
named volume (`data_vector_config`), substituting `${CRON_EVENTS_INGEST_TOKEN}`
with the minted token. Vector mounts that volume read-only and `--config`s the
rendered `/etc/vector/vector.toml`. `depends_on: monitor: service_healthy`
guarantees the render finishes before Vector starts — so a first-ever
`docker compose up -d` works with zero manual token-paste steps. This mirrors
the Alertmanager render-on-boot mechanism (`kernel/alertmanager/render.py`); the
cron version is `kernel/cron/render.py` and skips the `/-/reload` step because
Vector reads its config fresh at container start.

The rendered config is group-owned by GID 2000 (`amconfig`) at mode `0640`;
the Vector service joins that group via `group_add` so it can read the file
without the bearer token being world-readable.

---

## Audit verbs

| Verb                  | Who              | Context            | Effect |
|-----------------------|------------------|--------------------|--------|
| `cron.observed_run`   | `system:log-scrape` | B-mode log evidence (vanilla cron dispatch line) | `observed_runs_total++`, `last_observed_run_at` set, `current_state` UNCHANGED |
| `heartbeat.ok`        | `system:log-scrape` (if wrapper-tagged) | Wrapper-emitted `exit=0` | `current_state` ← `ok`, audit row created |
| `heartbeat.fail`      | `system:log-scrape` (if wrapper-tagged) | Wrapper-emitted `exit=N`, N≠0 | `current_state` ← `fail`, audit row created |

For all three: `cron_log_cursors` table gets a row to prevent replay idempotency.

---

## Cross-references

- Implementation:
  - `apps/monitor/homelab_monitor/kernel/cron/log_match.py` — `canonical_log_key()`
  - `apps/monitor/homelab_monitor/kernel/cron/log_event.py` — `CronLogEvent` dataclass
  - `apps/monitor/homelab_monitor/kernel/api/routers/cron_events.py` — ingest endpoint
  - `apps/monitor/alembic/versions/0012_logscrape_columns.py` — schema migration
- Config:
  - `deploy/vector/vector.toml.template` — Vector config template (rendered to
    `/etc/vector/vector.toml` at monitor boot with the ingest token)
  - `apps/monitor/homelab_monitor/kernel/cron/render.py` — render-on-boot
  - `deploy/compose/docker-compose.yml` — Vector service + `data_vector_config`
    volume
- Related docs:
  - `docs/architecture/cron-identity.md` — disk-side fingerprinting and discovery
  - `epics/EPIC-002-heartbeat-cron/STAGE-002-008.md` — stage tracking
  - Global design spec: `docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` §4.2 (B-mode)

---

## Delivery semantics — at-most-once (known limitation)

The cron-events ingest pipeline is **at-most-once**, not exactly-once.

`POST /api/internal/cron-events` processes each event in two separate database
transactions:

1. `CronRepo.try_claim_cursor` — `INSERT OR IGNORE` into `cron_log_cursors`,
   committed immediately.
2. `HeartbeatRepo.record_observed_run` / `record_ok` / `record_fail` — the
   state write + audit row, committed in its own later transaction.

If the process crashes in the window between commit 1 and commit 2, the cursor
is marked processed but the run is never recorded. Vector retries the POST, the
endpoint sees the cursor as already-claimed, returns `replay_skipped`, and the
run is dropped permanently.

This is an accepted trade-off: cron observed-run evidence is advisory (it does
not on its own gate alerting), the crash window is sub-millisecond on this
single-host single-writer SQLite deployment, and an exactly-once fix would
require threading a single open connection through the `HeartbeatRepo` public
mutators and every heartbeat-receiver caller. If exactly-once becomes required,
the fix is to claim the cursor LAST inside the same transaction as the state
write.
