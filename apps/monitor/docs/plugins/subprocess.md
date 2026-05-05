# Subprocess plugins

This document is for plugin authors. It describes how to write a subprocess
plugin for homelab-monitor: the manifest schema, the JSON line protocol,
trust tiers, and a worked example.

## Manifest schema (plugin.yaml)

A subprocess plugin is a directory containing a `plugin.yaml` manifest and
the executable(s) the manifest points to. Schema version 1:

| Field | Type | Required | Notes |
|---|---|---|---|
| `manifest` | int (literal `1`) | yes | Schema version. |
| `name` | string | yes | Matches `^[a-z][a-z0-9_-]{2,63}$`. Globally unique. |
| `language` | string | no | Informational only. Default `"bash"`. |
| `command` | list[string] | yes | argv. Resolved relative to manifest dir. Min length 1. |
| `interval` | duration | yes | How often the plugin runs. Min `5s`. |
| `timeout` | duration | yes | Max run time per tick. Must be `< interval`. |
| `concurrency_group` | string | no | Default `"default"`. Plugins in the same group run serially. |
| `trust_level` | enum | no | `trusted` (default) or `untrusted`. `builtin` is rejected. |
| `env` | dict[str, str] | no | Extra env vars passed to the subprocess. |
| `secrets` | list[string] | no | Names of secrets the plugin is allowed to receive. |
| `workdir` | string \| null | no | Override default cwd (manifest dir). |

**Duration format:** integer seconds (`60`) or `<int><s|m|h>` (`60s`, `5m`,
`1h`). Mixed units (e.g. `1h30m`) are not supported.

**Validation:** `extra` fields are forbidden. Invalid manifests are skipped
at load time with a warning log; one bad manifest does not block siblings.

## JSON line protocol

The subprocess receives a single JSON object on stdin (with `collector_name`,
`deadline_unix`, `secrets`), then closes stdin. The plugin must drain stdin
even if it doesn't use the contents (`cat >/dev/null`) to avoid SIGPIPE.

The plugin emits one JSON object per line on stdout. Five line types are
recognized:

### `metric`

Emit a metric for VictoriaMetrics ingestion.

```json
{"type":"metric","name":"<metric_name>","kind":"counter","value":<number>,"labels":{"<k>":"<v>",...}}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Prometheus-compatible metric name. |
| `kind` | string | no | `"counter"` (default), `"gauge"`, or `"summary"`. |
| `value` | number | yes | The metric value. |
| `labels` | dict[str, str] | no | Label set. Default `{}`. |

### `event`

Emit a `CollectorEvent`. The full event payload (minus the outer `"type":"event"`
wrapper) must validate against the `CollectorEvent` discriminated union.

```json
{"type":"event","kind":"suggestion","title":"...","body":"..."}
```

### `log`

Forward a log line to VictoriaLogs.

```json
{"type":"log","stream":"<stream_name>","line":"..."}
```

### `heartbeat`

Emit a heartbeat for the heartbeat receiver. STAGE-002-* will route these to
the dedicated receiver; for STAGE-009 they are appended to `result.events`.

```json
{"type":"heartbeat","source":"<source_name>"}
```

### `result` (terminal)

The final line. Indicates success/failure. Lines emitted
*after* a `result` are logged at warning level and discarded.

```json
{"type":"result","ok":true,"errors":[]}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `ok` | bool | yes | Outcome flag. |
| `errors` | list[string] | no | Error strings; default `[]`. |

If the plugin exits without emitting a `result` line, the runner synthesizes
a failure with `errors=["no result line emitted"]`.

If the plugin exits non-zero AFTER emitting `ok=true`, the runner overrides
`ok` to `false` and appends a `non-zero exit code: N` error.

### Error handling

| Condition | Runner behavior |
|---|---|
| Malformed JSON | Warning log; skip line; continue parsing. |
| JSON not an object | Warning log; skip; continue. |
| Unknown `type` | Info log; skip; continue. |
| Line after `result` | Warning log; skip. |
| Empty / whitespace-only line | Silent skip. |
| Spawn failure (e.g. ENOENT) | `ok=false` with `subprocess spawn failed: ...` error. |
| Timeout | `SIGTERM` → 2s grace → `SIGKILL` to process group; `ok=false` with `timeout after Ns` error string. |

## Trust tiers

`trusted` (default) and `untrusted` plugins both run with:

- A scrubbed environment containing only manifest-declared `env` keys plus
  the allowlist (`PATH`, `TZ`) and a synthesized `HOME=/tmp/<plugin-name>`.
- Secrets filtered to the manifest's `secrets` declaration. Undeclared
  secrets are not visible.
- A working directory of the manifest's parent directory (or `workdir`
  override).
- No DB write capability — the protocol simply has no DB-write line type.

**Note on `$HOME`:** `$HOME` is set to `/tmp/<plugin-name>` for untrusted plugins, but the directory is NOT pre-created by the runner. Plugins requiring a writable home must create it themselves:

```bash
mkdir -p "$HOME"
```

This avoids the runner needing to clean up after every subprocess; plugin authors maintain control over their workspace lifecycle.

Future hardening will diverge: `untrusted` will gain cgroups, RLIMIT, and
namespace isolation.

## Worked example: bash hello-world

Located at `runbooks/_examples/hello-subprocess-plugin/`.

`plugin.yaml`:
```yaml
manifest: 1
name: hello-subprocess
language: bash
command: ["./run.sh"]
interval: 60s
timeout: 10s
trust_level: trusted
env: {}
secrets: []
```

`run.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null  # drain stdin to avoid SIGPIPE
echo '{"type":"metric","name":"homelab_hello_world","kind":"counter","value":1,"labels":{"language":"bash"}}'
echo '{"type":"result","ok":true}'
```

This plugin:

1. Drains its stdin (the runner's config + secrets payload).
2. Emits one counter metric.
3. Emits a successful `result` line.
4. Exits 0.

The runner returns `CollectorResult(ok=True, metrics_emitted=1)`.
