# Log Redaction

Homelab-monitor strips sensitive patterns out of log message text **before** the
logs reach VictoriaLogs (VL). This is your defense against bearer tokens, JWTs,
passwords-in-URLs, and API keys leaking into a queryable, long-retention log store.

This page is for operators who want to confirm redaction is working or add their
own patterns.

## What redaction does

Vector (the log shipper) runs a redaction transform on every log line's
`.message` field. When a line matches a configured pattern, the matched text is
replaced with a fixed placeholder (e.g. `Bearer [REDACTED]`) **inside Vector**,
before the line is shipped to VictoriaLogs. The original secret never reaches VL
and is never written to disk in the log store.

Key properties:

- **Redaction happens in-flight.** It rewrites `.message` in Vector's pipeline
  between log ingest and the VL sink. Both the docker/journald log path
  (`redact_main`) and the cron run-capture path (`redact_hmrun`) run the
  byte-identical redaction VRL.
- **Counts-only observability.** Each match increments a Prometheus counter,
  `vector_redactions_total`, labeled with the static pattern name
  (`pattern_type`). The collector and audit trail record *how many* lines matched
  each pattern — **never the matched secret value**.
- **No secret ever appears in metrics, logs, or the audit table.** The
  `pattern_type` label is a fixed literal (the pattern's `name`), not the matched
  text.

## The 5 default patterns

These ship with the public release and apply automatically when
`homelab-monitor.yaml` does not define `logs.redact` (see
[Adding a custom pattern](#adding-a-custom-pattern) for the override rules).

| Name | What it catches | Regex (Rust-regex / Vector VRL) | Replacement |
| --- | --- | --- | --- |
| `bearer_token` | `Authorization: Bearer <token>` headers (token ≥ 20 chars) | `(?i)bearer\s+[A-Za-z0-9._-]{20,}` | `Bearer [REDACTED]` |
| `jwt` | JSON Web Tokens (`eyJ...` three dot-separated base64url segments) | `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` | `[REDACTED_JWT]` |
| `password_in_url` | `user:password@host` credentials embedded in a URL | `://[^:@/\s]+:[^@/\s]+@` | `://[REDACTED]:[REDACTED]@` |
| `aws_access_key` | AWS access key IDs (`AKIA` + 16 uppercase alphanumerics) | `AKIA[0-9A-Z]{16}` | `[REDACTED_AWS_KEY]` |
| `api_key_generic` | `api_key=`, `api-token=`, `access_token=`, `secret_key=` followed by a ≥ 16-char value | `(?i)(api[-_]?key\|api[-_]?token\|access[-_]?token\|secret[-_]?key)["']?\s*[:=]\s*["']?[A-Za-z0-9._-]{16,}` | `${1}=[REDACTED]` |

Notes:

- `bearer_token` requires the token to be **≥ 20 chars** so benign phrases like
  `Bearer password` are not redacted. This is a deliberate bias toward
  over-redaction of real tokens without nuking ordinary prose.
- `api_key_generic`'s replacement `${1}=[REDACTED]` is a **capture-group
  backreference**: `${1}` is the matched key name (e.g. `api_key`), so the output
  keeps the key name visible and only redacts the value:
  `api_key=AKIAEXAMPLEvalue1234` → `api_key=[REDACTED]`.

## Adding a custom pattern

Custom patterns live in `homelab-monitor.yaml` under the `logs.redact:` key. The
file is located via the `HOMELAB_MONITOR_CONFIG` environment variable, which
defaults to `/config/homelab-monitor.yaml` inside the monitor container.

`logs.redact` is a **list** of `{name, pattern, replacement}` mappings:

```yaml
logs:
  redact:
    - name: bearer_token
      pattern: '(?i)bearer\s+[A-Za-z0-9._-]{20,}'
      replacement: "Bearer [REDACTED]"
    - name: jwt
      pattern: 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
      replacement: "[REDACTED_JWT]"
    # ... your custom pattern below ...
    - name: my_internal_token
      pattern: '(?i)x-internal-token:\s*[A-Za-z0-9]{24,}'
      replacement: "x-internal-token: [REDACTED]"
```

### Important: an explicit list REPLACES the defaults — it does not append

This is the single most common mistake. The behavior is:

| `logs.redact` in YAML | Result |
| --- | --- |
| Key (or `logs:` section, or whole file) **absent** | All 5 defaults apply |
| `redact:` present but with an empty/null value | All 5 defaults apply |
| `redact: []` (explicit empty list) | **Redaction OFF** — zero patterns |
| `redact: [ ...entries... ]` (non-empty list) | **Only your listed entries apply — the 5 defaults are dropped** |

So if you want the defaults **plus** a custom pattern, you must copy all 5
default patterns into your `logs.redact` list and add your pattern alongside
them (as shown in the YAML example above). Listing only `my_internal_token`
would disable bearer/jwt/password/aws/api-key redaction.

### Applying the change

Patterns are rendered into Vector's config (`vector.toml`) **at monitor boot**.
After editing `homelab-monitor.yaml` you must **restart the monitor** so it
re-renders `vector.toml` and Vector restarts with the new redaction VRL:

```bash
docker compose restart monitor vector
```

(Vector reads the freshly rendered config at container start — there is no live
reload.)

> If the config is invalid, the monitor logs `vector.redact.config_invalid` at
> ERROR and renders an **empty** redaction block for that boot — meaning
> **redaction is NOT applied** until you fix the config and restart. The monitor
> itself still boots; it does not crash on a bad redaction config. Always check
> the metrics after a config change (see
> [Verifying redaction works](#verifying-redaction-works)).

## Validation rules

`logs.redact` entries are validated when the monitor boots. A violation makes
redaction config loading fail loudly (logged as `vector.redact.config_invalid`),
and that boot ships logs **without** redaction. Fix the config and restart.

Each entry must satisfy:

| Rule | Invalid example | Why |
| --- | --- | --- |
| All three fields present and non-empty | missing `replacement`; `name: ""` | `name`, `pattern`, `replacement` are all required, must be non-empty strings |
| `name` is lowercase snake_case | `name: "Bearer Token"` | `name` must match `^[a-z][a-z0-9_]*$` — it becomes both a Prometheus label and a VRL field |
| `name` is unique | two entries named `dup` | duplicate names are rejected |
| `pattern` contains no lookarounds | `(?=foo)bar`, `(?!foo)`, `(?<=foo)`, `(?<!foo)` | Vector uses the Rust `regex` crate, which does not support lookaround |
| `logs.redact` is a list; each entry is a mapping | `redact: 42`; `redact: ["just_a_string"]` | structure must match `list[{name,pattern,replacement}]` |

### Regex dialect notes (Rust regex / Vector VRL)

- **No lookarounds.** `(?=`, `(?!`, `(?<=`, `(?<!` are rejected at config load.
  Use positive enumeration instead.
- **Inline flags are supported.** `(?i)` (case-insensitive) and other inline flag
  groups work — the defaults use `(?i)` for `bearer_token` and `api_key_generic`.
- **Capture backreferences in `replacement`.** Use `${1}`, `${2}`, etc. to
  reference capture groups from your `pattern` (as `api_key_generic` does with
  `${1}`). Write the backref literally as `${1}` in the YAML — the renderer
  escapes the `$` correctly so Vector interprets it as a VRL `replace()` capture
  reference, not as a config-time variable.
- **A literal single quote** in your pattern is handled automatically — the
  renderer encodes it as `\x27` because Vector's raw-string regex form (`r'...'`)
  has no escape character. You do not need to do anything special; just write the
  `'` normally in YAML.

## Verifying redaction works

Redaction is observable through two surfaces: the Prometheus counter and the
audit log.

### 1. The `vector_redactions_total` metric

Vector exports the counter on its `prometheus_exporter` sink at `:9598`
(container-internal). The monitor scrapes it into VictoriaMetrics. The metric is
labeled by `pattern_type` (the pattern `name`).

Query it in Grafana or directly against VictoriaMetrics:

```bash
# Per-pattern cumulative match counts (instant query against VictoriaMetrics).
# In dev the VM API is published on 127.0.0.1:18428; in prod query it via the
# monitor's proxy or from inside the compose network at victoriametrics:8428.
curl -s 'http://127.0.0.1:18428/api/v1/query?query=vector_redactions_total' \
  | jq '.data.result[] | {pattern_type: .metric.pattern_type, count: .value[1]}'
```

Useful MetricsQL:

```promql
# Total matches per pattern over the last hour
sum by (pattern_type) (increase(vector_redactions_total[1h]))

# Confirm a specific pattern is firing
vector_redactions_total{pattern_type="bearer_token"}
```

To functionally test a new pattern: emit a log line from a container that the
pattern should match, wait a few seconds for Vector to process it, then re-query.
The `pattern_type` series for your pattern should increment.

### 2. The redaction audit log

The `redaction_audit` collector runs every 5 minutes. It queries
`vector_redactions_total` from VictoriaMetrics, diffs each `pattern_type` against
the previous tick, and — when any pattern fired since the last tick — writes **one
counts-only row** to the audit log:

- `who = "system"`
- `what = "logs.redaction_counts"`
- payload: per `pattern_type`, `{delta, cumulative}` counts only

Look for `logs.redaction_counts` rows in the audit table / audit UI to see which
patterns matched and how often over time. The payload contains **counts only —
never any matched text**.

> After a monitor restart the collector's in-memory "last seen" resets, so the
> first post-restart tick reports `delta == cumulative` for any active pattern.
> This is expected and keeps the cumulative column monotonic.

## Security notes

- **Secrets never reach VictoriaLogs.** Redaction rewrites `.message` upstream of
  the VL sink. The redaction marker fields (`.rdt_<name>`) used internally to
  drive the counter are stripped (`del(.rdt_<name>)`) before the line is shipped,
  so they never appear in VL either.
- **`pattern_type` labels are static.** The metric label is the pattern's `name`
  literal, fixed at config time — it is **never** the matched secret.
- **The audit trail records counts, not values.** `logs.redaction_counts` rows
  hold per-pattern deltas and cumulatives only.
- **The redaction collector never writes to VictoriaLogs** — it only reads the
  counter from VictoriaMetrics — which avoids any chance of a redaction-loop
  feeding redacted-about-redaction text back into the log store.
- **Bias toward over-redaction.** Patterns are tuned to err on the side of
  redacting (e.g. `bearer_token` requires a ≥ 20-char token to avoid both false
  negatives on real tokens and false positives on benign words). When in doubt, a
  pattern redacts.

### Known gap: no retroactive redaction

Redaction applies **only to log lines ingested after a pattern is active**. Log
lines already stored in VictoriaLogs before you added (or fixed) a pattern are
**not** retroactively scrubbed. If a secret was ingested before a matching
pattern existed, it remains in VL until it ages out under VL retention. To purge
it sooner you must delete the affected log data in VictoriaLogs directly.
