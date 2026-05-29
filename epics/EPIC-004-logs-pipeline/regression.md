# Regression Checklist - EPIC-004: Logs pipeline

(Items added per stage during Refinement.)

## STAGE-004-001 — Vector multi-line log stitching

- Run `make integration` — `test_vector_multiline.py` must pass all 11 tests (9 languages stitch into single VL records with tail fragments present; 2 negative controls stay separate).
- `make uv ARGS="--directory apps/monitor pytest tests/test_vector_template.py"` — multiline structural + regex tests pass; `test_test_fixture_multiline_matches_template` (drift guard) confirms `deploy/compose/test-fixtures/vector.toml` multiline block is byte-identical to `deploy/vector/vector.toml.template`.
- Vector config must use `mode = "continue_through"` (NOT halt_before) on `[sources.docker_logs.multiline]`. NO `[sources.journald.multiline]` block (Vector's journald source rejects it → crash-loops the whole config).
- After any vector.toml.template change in prod: recreate monitor with `--force-recreate` so the boot-time render writes the new config; confirm `docker exec homelab-vector cat /etc/vector/vector.toml` shows the change AND vector is `Up` (not Restarting).
- Vector patterns MUST contain no lookarounds (`(?!`, `(?=`, `(?<!`, `(?<=`) — Rust `regex` crate rejects them. Enforced by `_assert_no_lookarounds` in test_vector_template.py.

## STAGE-004-002 — LogLine shape convergence

- All 3 log endpoints (`/api/integrations/docker/containers/{name}/logs`, `/api/crons/{fp}/runs/{run_id}/log`, `/api/logs/query`) MUST return `.lines: list[LogLine]` where `LogLine = {timestamp, message, stream, severity, host, service, fields}`. NO `entries` outer field; NO `ContainerLogLine`/`RunLogLine`/`LogsQueryEntry` inner types.
- `make uv ARGS="--directory apps/monitor pytest tests/test_logline_model.py"` — mapper branch matrix (severity numerics/aliases/canonical/unknown→info/None, host/service extraction+fallback, severity_raw preservation, input-not-mutated) must pass at 100%.
- Severity is normalized at the mapper to canonical lowercase: debug/info/notice/warn/error/critical/alert/emergency. Raw value preserved in `fields.severity_raw`.
- `make integration` — `test_vector_to_vl_path` + `test_vector_multiline` (×11) must read `.lines[].message` from real VL data without shape errors. A field rename here breaks both the frontend component mocks AND these integration assertions — update all when changing the shape.
- After ANY change to the LogLine shape or the 3 response models: regenerate `make openapi-export` + `bash scripts/generate-ui-types.sh`, and grep `apps/ui/src/**/__tests__` for stale `entries:`/`line:` mock fields.
