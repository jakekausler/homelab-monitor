# Regression Checklist - EPIC-004: Logs pipeline

(Items added per stage during Refinement.)

## STAGE-004-001 — Vector multi-line log stitching

- Run `make integration` — `test_vector_multiline.py` must pass all 11 tests (9 languages stitch into single VL records with tail fragments present; 2 negative controls stay separate).
- `make uv ARGS="--directory apps/monitor pytest tests/test_vector_template.py"` — multiline structural + regex tests pass; `test_test_fixture_multiline_matches_template` (drift guard) confirms `deploy/compose/test-fixtures/vector.toml` multiline block is byte-identical to `deploy/vector/vector.toml.template`.
- Vector config must use `mode = "continue_through"` (NOT halt_before) on `[sources.docker_logs.multiline]`. NO `[sources.journald.multiline]` block (Vector's journald source rejects it → crash-loops the whole config).
- After any vector.toml.template change in prod: recreate monitor with `--force-recreate` so the boot-time render writes the new config; confirm `docker exec homelab-vector cat /etc/vector/vector.toml` shows the change AND vector is `Up` (not Restarting).
- Vector patterns MUST contain no lookarounds (`(?!`, `(?=`, `(?<!`, `(?<=`) — Rust `regex` crate rejects them. Enforced by `_assert_no_lookarounds` in test_vector_template.py.
