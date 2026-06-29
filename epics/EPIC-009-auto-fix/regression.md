# Regression Checklist - EPIC-009: Auto-fix

(Items added per stage during Refinement.)

## STAGE-009-001 — Runbook schema & config-file contract

- [ ] Migration `0045_runbook_schema.py` applies up to head on a fresh SQLite DB and adds all 8 `runbooks` columns (alert_match_patterns, risk_tag default 'risky', dry_run_required default 1, rate_limit_per_hour, cooldown_seconds, enabled default 0, auto_trigger default 0, content_hash) and all 10 `runbook_runs` columns (alert_id FK→alerts.id, mode, prompt, transcript_path, exit_code, started_at, ended_at, fixer_user, host, runbook_hash). Verify via `PRAGMA table_info` on both tables + `PRAGMA foreign_key_list(runbook_runs)` shows the alerts FK.
- [ ] Migration downgrades cleanly to `0044` (all 18 new columns gone; original stub columns + `runbook_id→runbooks.id` FK preserved).
- [ ] Conservative open-source-safe defaults hold when a real runbook YAML omits them: `RunbookConfig.load_from_path` yields `risk_tag=RISKY` and `dry_run_required=True`.
- [ ] Safety gate (non-negotiable #2 scope): a runbook config whose `scoped_capabilities` declares NEITHER `docker` NOR `ssh` is REJECTED by `RunbookConfig` with a `ValueError` ("must declare at least one of 'docker' or 'ssh'"). Egress-only is not a valid scope.
- [ ] `RunbookConfig.load_from_path` rejects malformed files (missing `scoped_capabilities`; non-mapping YAML root; unknown extra top-level field via extra=forbid) with a `ValueError` that includes the file path.
- [ ] `compute_runbook_content_hash` is YAML-format-agnostic (same semantic config in different formatting → identical hash) and sensitive to semantic change (mutating a field → different hash).
- [ ] **STAGE-009-012 follow-up (deferred from 001 Design):** decide whether per-run drift detection (`runbook_hash`) must also cover markdown-intent changes (whole-folder hash incl. `*.md`), or remain config-only (current 001 behaviour = canonical-config hash only).
