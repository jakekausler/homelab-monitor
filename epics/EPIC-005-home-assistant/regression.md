# Regression Checklist - EPIC-005: Home Assistant

(Items added per stage during Refinement.)

## STAGE-005-001 (HA REST client)

- **STAGE-005-001:** With the prod rig up and `ha_token` set, the HomeAssistantRestClient must reach real HA: `get_config()` returns a non-empty HA version + time_zone (not an HaError); a bad/missing token yields `HaError(reason="auth")` (HTTP 401) with the token never appearing in the error message. `load_ha_config()` reads `HOMELAB_MONITOR_HA_URL` (default `http://192.168.2.148:8123`). Validate by constructing the client inside the monitor container (no HA collector/endpoint exists until later stages).
