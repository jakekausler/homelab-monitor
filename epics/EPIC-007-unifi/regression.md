# Regression Checklist - EPIC-007: Unifi

(Items added per stage during Refinement.)

## STAGE-007-001 (Unifi API client)

- **STAGE-007-001 (Unifi API client):** With the real `unifi_api_key` (read-only) set and the live UDM reachable, the client must reach BOTH API surfaces over self-signed TLS (`verify=False`): `v1_sites()` → 200 with a site UUID; `resolve_site_id()` caches the UUID into `v1_site_id` while `site_name` stays `"default"`; `stat_sysinfo()` (classic, uses `site_name`) → 200; `v1_devices()` (v1 site-scoped, uses `v1_site_id`) → 200. The API key must NEVER appear in logs or error messages. Regression guard: classic URLs must use the short site NAME, not the v1 UUID (a UUID in the classic path 401s).
