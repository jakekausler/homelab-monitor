# Regression Checklist - EPIC-008: Synology

(Items added per stage during Refinement.)

## STAGE-008-001 — Synology DSM v7 client

- [ ] **Live auth + system info:** The real `SynologyRestClient` (base_url `https://192.168.2.4:5001`, account `homelab-monitor`, secret `synology_dsm_password`) authenticates against the live DSM (`SYNO.API.Auth` v7 login, **NO `session=` param**) and `system_info()` returns a `SynologyResponse` with real data (model `DS3622xs+`, firmware string, uptime, sys_temp) + a `took_seconds` float. (Sending a `session=` param returns DSM error 402 — do NOT add it.)
- [ ] **Session reuse:** A second client call reuses the `_sid` without re-login (only ONE `SYNO.API.Auth method=login` across two calls).
- [ ] **Logout frees the session:** `aclose()` issues `SYNO.API.Auth method=logout` and never raises even if the NAS is unreachable at shutdown.
- [ ] **119 re-auth:** A DSM body error code 119 (session expired, on HTTP 200) triggers exactly ONE re-auth + retry; a second 119 surfaces `SynologyError(reason="auth", status=119)`.
- [ ] **Self-signed TLS:** the dedicated Synology httpx client uses `verify=False` (DSM cert `CN=synology`).
- [ ] **DSM password never logged:** no Synology error message or log line ever contains the password or the `_sid`.
- [ ] **Cron fixtures present:** `apps/monitor/tests/data/cron_fixtures/{system_cron,user_crontab,reboot_only}.example` exist + are git-tracked (the `!apps/monitor/tests/data/` `.gitignore` exception) so the 3 cron-parser tests pass on a clean checkout. (Pre-existing failure fixed in this stage.)

## STAGE-008-002 — Synology integration bundle skeleton

- [ ] **Bundle registers:** `register_all` (called from lifespan after the Unifi registration) registers `synology_placeholder` via `loader.load_all()` with config name `synology_placeholder`, interval 60s, timeout 5s, concurrency_group `default` (NOT `synology`).
- [ ] **Defensive isolation:** a raising `loader.register` for one collector is logged (`synology_integration.collector_register_failed`) and does NOT propagate (other collectors still register).
- [ ] **Placeholder emits:** `SynologyPlaceholderCollector.run()` returns ok=True, metrics_emitted==1, emits `homelab_synology_bundle_loaded=1.0`, and never touches `ctx.synology`.
- [ ] **Placeholder is scaffolding:** `placeholder.py` + the `_SYNOLOGY_COLLECTORS` entry + `test_synology_placeholder_collector.py` are REMOVED in STAGE-008-005 (the first Wave-B collector). After 008-005, `homelab_synology_bundle_loaded` no longer emits.
- [ ] **No `_shared.py` yet:** `integrations/synology/_shared.py` is created by STAGE-008-003 (not 008-002).

## STAGE-008-003: Synology `_shared.py` helpers + cardinality-cap hook

- [ ] **`_shared.py` helper unit suite green:** `make uv ARGS="--directory apps/monitor pytest tests/test_synology_shared.py"` passes (currently 14+ tests, 100% branch coverage of `_shared.py`). Covers `client_unconfigured_result`, `failed_result`, `fetch_or_result` (success + error branches), `as_float` (all branches incl. bool/None/non-finite rejection), `bytes_field`/`percent_field`, `cap_for_synology`, `capped_emitter`.
- [ ] **API-latency metric name is stable:** `_shared.M_API_TOOK_SECONDS == "homelab_synology_api_took_seconds"`; `fetch_or_result` emits it with a single `{api=<endpoint>}` label on every successful fetch. Any Synology collector relying on this gauge must keep the name + label key in sync.
- [ ] **`ok=True`-when-NAS-sad convention preserved:** `fetch_or_result` returns the `SynologyResponse` (NOT a failed `CollectorResult`) whenever the client returns a `SynologyResponse`, regardless of degraded payload fields (e.g. volume status "has_unverified_disk"). Only a `SynologyError` produces `ok=False`. Collectors own the per-field state decision.
- [ ] **Cardinality cap is the default 500 (guardrail, not active limit):** `cap_for_synology("homelab_synology_<any>")` returns 500 (no `homelab_synology_*` entries in `_DEFAULT_CARDINALITY_FAMILIES` — intentional). `capped_emitter(...).emit_family(...)` always writes one `homelab_metric_family_dropped_series` gauge and appends a `SuggestionEvent(severity="warning")` only when series exceed the cap. Verified: 8 obs → 0 dropped; 600 obs → 100 dropped + 1 warning event.
- [ ] **3b live exercise owed by STAGE-008-005:** `_shared.py` helpers are first exercised against the LIVE DSM in STAGE-008-005 (storage collector). Confirm there.
- [ ] **Collector-author contract notes (for STAGE-008-005+):** use `emitted: list[int] = [0]` and accumulate across `fetch_or_result` calls (mutable-box is intentional); call `capped_emitter` once per `run()` tick with the same `events` list returned in the `CollectorResult`; count the always-written drop gauge toward `metrics_emitted` (e.g. `emit_family` return + 1 per family). `bytes_field`/`percent_field` are thin `as_float` aliases — the extension points if a field later needs unit conversion.
