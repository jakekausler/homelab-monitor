# Regression Checklist - EPIC-005: Home Assistant

(Items added per stage during Refinement.)

## STAGE-005-001 (HA REST client)

- **STAGE-005-001:** With the prod rig up and `ha_token` set, the HomeAssistantRestClient must reach real HA: `get_config()` returns a non-empty HA version + time_zone (not an HaError); a bad/missing token yields `HaError(reason="auth")` (HTTP 401) with the token never appearing in the error message. `load_ha_config()` reads `HOMELAB_MONITOR_HA_URL` (default `http://192.168.2.148:8123`). Validate by constructing the client inside the monitor container (no HA collector/endpoint exists until later stages).

## STAGE-005-002 (HA websocket client)

- **STAGE-005-002 (HA websocket client):** With the prod rig up and `ha_token` set, the `HomeAssistantWebsocketClient` must reach real HA `/api/websocket`: the auth handshake completes (`connected` becomes True), `send_command("get_config")` returns the HA version (not an HaError), and `subscribe("subscribe_events", event_type="state_changed")` yields at least one real event. `homelab_ha_websocket_connected` gauge=1.0 + `homelab_ha_websocket_reconnect_total>=1` after connect. `stop_task()` stops cleanly with no hang. Validate via an in-container async snippet (no WS collector/endpoint until stages 010/011/012). **Constraint:** never use a WS `get_states` command for bulk entity fetch — it exceeds HA's 1MB frame limit (1009 close); use the REST client's `get_states()` instead.
