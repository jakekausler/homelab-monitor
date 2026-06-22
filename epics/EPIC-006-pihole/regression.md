# Regression Checklist - EPIC-006: Pi-hole

(Items added per stage during Refinement.)

## STAGE-006-001 — Pi-hole v6 client

- [ ] **Live auth + version read:** From inside the prod `homelab-monitor` container, the real `PiholeRestClient` (base_url `http://192.168.2.148:8080`, secret `pihole_api_password_ro`) authenticates (`POST /api/auth` → SID) and `info_version()` returns a `PiholeResponse` with real version data (Core/Web/FTL) + a `took_seconds` float. (NOT `localhost` — the bridge-network container cannot reach the host's loopback.)
- [ ] **Session reuse:** A second client call (e.g. `info_ftl()`) reuses the SID without re-login (only ONE `POST /api/auth` across both calls).
- [ ] **Logout frees the slot:** `aclose()` issues `DELETE /api/auth` and never raises even if Pi-hole is unreachable at shutdown.
- [ ] **Two secrets stored:** Both `pihole_api_password_ro` and `pihole_api_password_rw` exist in the prod secret store (same value; Pi-hole v6 single app-password tier; RW first exercised in STAGE-006-018).
- [ ] **Vector label:** VictoriaLogs `service:"pihole-unbound"` still returns live FTL log hits (scope for STAGE-006-017 + 006-024).
- [ ] **base_url default is host LAN IP:** `load_pihole_config().base_url` defaults to `http://192.168.2.148:8080` (overridable via `HOMELAB_MONITOR_PIHOLE_URL`), NOT `localhost`.
- [ ] **App password never logged:** No Pi-hole error message or log line ever contains the app-password value.
