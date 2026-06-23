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
