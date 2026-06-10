# EPIC-021: Special-case monitoring for other Docker containers

## Status: Not Started (0 / 7 Complete)

## Stages Counter: 0 / 7 Complete

## Current Stage: STAGE-021-001

## Current Phase: STAGE-021-001 Design (Not Started)

## Overview

The dedicated integration epics cover the "big" services — Home Assistant (EPIC-005), Pi-hole
(EPIC-006), Unifi (EPIC-007), Synology (EPIC-008) — and EPIC-018 (per-service deep-dives) owns the
named appliance/protocol services (Mosquitto, Zigbee2MQTT, Z-wave JS, Plex, Frigate, Foundry,
Music Assistant, Node-RED, Grocy, rtlamr2mqtt, ip-update, host-native MariaDB/MySQL, Reolink, UPS,
Surveillance Station). That still leaves a set of the user's **custom-built application containers**
with no monitoring home. EPIC-021 gives each of those a "special-case" monitoring bundle: an HTTP
health probe, a log-pattern alert rule, and dependency checks — following the EPIC-003 container-probe
pattern and the EPIC-005 per-integration-panel/registration pattern.

These are bespoke apps the user wrote (local Docker builds, not upstream images), so they have no
upstream digest watching and often no native HEALTHCHECK; they need app-level liveness + dependency
monitoring (e.g. "is the host MySQL it depends on reachable?", "is the `/rackstation` NFS mount it
reads present?"). The epic also covers the disabled-by-profile apps gracefully (probe only when the
profile is active; rules stay silent while disabled).

This epic is sequenced LATE (after the integration exemplar EPIC-005 and the deep-dive EPIC-018
establish the container-probe + panel patterns) and is largely a breadth exercise: apply a known
pattern to each remaining container.

## Source documents (read before starting any stage)

- Master design spec §2 Q13 (Docker monitoring: per-container "specific" probes by label/config),
  §3.4 (discovered/probed targets), §5 (collector framework), §9.2 (Integrations panels).
- Project memory `reference_docker_inventory.md` (active vs disabled compose services + per-container
  monitoring notes; host-network containers; `/rackstation` NFS dependencies).
- EPIC-003 (Docker collector + container HTTP/TCP probes + label-based probe config — the probe
  mechanism these stages configure per app).
- EPIC-005 (the per-integration UI panel + collector registration pattern; reuse, do not reinvent).
- The user's compose at `/storage/docker/compose/docker-compose.yml` (the authoritative service list +
  `profiles: ["disabled"]` markers). Re-verify the live inventory at epic Design time — containers
  come and go.

## Container inventory (audited 2026-06-10)

The user's primary compose defines ~36 services. After excluding everything owned by another epic,
the EPIC-021 candidates are the custom-built apps below. **The user confirmed: exclude the two
`test-caddy-*` containers (ephemeral test-harness artifacts) and the `campaign_*` / `gm-*` containers
(separate projects — out of scope for homelab-monitor entirely).**

### In scope — active custom apps

| Container | Build | Purpose | Monitoring angle |
|---|---|---|---|
| `grocy-homeassistant` | local | HA ↔ Grocy sync bridge | HTTP health (port 8246); custom app logs; dependency health (both Grocy AND HA reachable — if either fails the bridge breaks). |
| `kingdom-rules` | local | Kingmaker D&D ruleset web app | HTTP health (port 9035); app logs; uptime. |
| `library-organizer` | local | Library cataloging app | HTTP health (port 8181); **host MySQL :3306 dependency**; host-network mode; app logs (catalog-scan failures). |
| `bills` | local | Expense/billing tracker | HTTP health (port 5173); **host MySQL :3306 dependency**; app logs. |
| `podcast-feed` | local | YouTube/podcast aggregator | HTTP health (port 8585); **`/rackstation` NFS-mount dependency**; per-source scan status in logs. |
| `udo-viewer` | local | UDO dashboard | HTTP health (port 14572); app logs; uptime. (Purpose to confirm at Design — discover what it connects to.) |

### In scope — disabled-by-profile (monitor only when the profile is active)

| Container | Build | Purpose | Notes |
|---|---|---|---|
| `language-tutor` | local | OpenAI-powered language teaching | port 8734; OpenAI API-key validity + LLM-reachability probes when enabled. |
| `deadlands-options` | local | Deadlands RPG module display | port 7492; app logs when enabled. |
| `ghost` + `ghost-db` | upstream (ghost + mariadb) | Blog platform + its own MariaDB | port 2368; DB connectivity; content-volume health. NOTE: `ghost-db` is a *container* MariaDB (distinct from the host-native MySQL/MariaDB that EPIC-018-010 owns). |

### Excluded (owned elsewhere — documented so future sessions don't double-cover)

- HA + matter-server → EPIC-005. pihole-unbound → EPIC-006. Unifi → EPIC-007. Synology/`/rackstation`
  mount discovery → EPIC-008.
- Mosquitto, zigbee2mqtt, zwave-js-ui, plex, frigate, foundry, music-assistant, nodered, grocy,
  rtlamr2mqtt, ip-update, Reolink, UPS, Surveillance Station → **EPIC-018** (per-service deep-dives).
- Host-native MariaDB/MySQL (serving `bills`/`library-organizer`) → **EPIC-018 STAGE-018-010**
  (EPIC-021 only does the *dependency reachability check* from the app's side, not the DB's own
  deep-dive).
- Monitor's own sidecars (victoriametrics, victorialogs, vmagent, vmalert ×2, alertmanager, karma,
  kthxbye, grafana, netdata, vector, cadvisor, local-watchdog, monitor, config-init) → **EPIC-014**
  (self-monitor).
- `test-caddy-1`, `test_pull_and_restart_case_ins0-caddy-1` → **excluded** (ephemeral test artifacts).
- `campaign_*`, `gm-*` → **excluded** (separate projects).

## Stage decomposition (7 stages, parallelizable)

Each active-app stage = configure the EPIC-003 container probe (HTTP health + label/config), add a
log-pattern vmalert-logs rule for that app's known error shapes, wire any dependency check, and add a
regression entry. Stages are independent and MAY be reordered. The disabled-apps stage is grouped
since they share "probe-only-when-active" handling.

| # | Stage | Theme |
|---|---|---|
| STAGE-021-001 | grocy-homeassistant bridge monitoring | HTTP health probe (8246); log-pattern rule; **dual-dependency** check (Grocy + HA reachable) — the bridge's whole job is mediating two services, so monitor both legs. |
| STAGE-021-002 | kingdom-rules monitoring | HTTP health probe (9035) + log-pattern rule + uptime. |
| STAGE-021-003 | library-organizer monitoring | HTTP health probe (8181); host-MySQL-:3306 reachability dependency check; host-network handling; catalog-scan-failure log rule. |
| STAGE-021-004 | bills monitoring | HTTP health probe (5173); host-MySQL-:3306 reachability dependency check; log-pattern rule. |
| STAGE-021-005 | podcast-feed monitoring | HTTP health probe (8585); `/rackstation` NFS-mount dependency check (mount present + writable); per-source scan-status log rule. |
| STAGE-021-006 | udo-viewer monitoring | HTTP health probe (14572) + log-pattern rule; Design phase confirms its actual purpose + dependencies. |
| STAGE-021-007 | Disabled-by-profile apps (language-tutor, deadlands-options, ghost+ghost-db) | Establish the "probe only when the compose profile is active; vmalert rules silent + UI shows 'disabled' when not" handling, then apply to all three. ghost includes container-MariaDB connectivity; language-tutor includes OpenAI-API-key/LLM-reachability checks. |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Probe failures degrade gracefully** — a custom app being down produces a clear `homelab_probe_up == 0`
  alert for that target, never a 5xx in our own API.
- **Disabled-by-profile is first-class** — a service with `profiles: ["disabled"]` (or stopped) does
  NOT produce false "down" alerts; rules are silent and the UI/inventory shows "disabled", not
  "error". (Mirrors the spec's "discoverer must handle both states".)
- **Dependency checks are explicit** — where an app depends on host MySQL or an NFS mount, the alert
  distinguishes "app down" from "app's dependency down" (so a MySQL outage doesn't look like six
  separate app crashes).
- **No upstream digest watching for local builds** — these are user-built images; image-update alerts
  (EPIC-003) do not apply. Use source/git-based change detection only if a stage's Design decides it's
  worth it (otherwise omit — YAGNI).

## Dependencies

- EPIC-003 (Docker collector + container probe mechanism this epic configures per app).
- EPIC-005 (per-integration panel + collector registration pattern; minimal severity routing for any
  push alerts).
- EPIC-018 (host-native MySQL/MariaDB deep-dive — EPIC-021's dependency checks reference it but do not
  duplicate it). Sequence EPIC-018 ahead where the DB-side monitoring is wanted.
- EPIC-008 (`/rackstation` mount discovery — podcast-feed's dependency check references the mount
  target).

## Notes

- **Re-verify the inventory at Design time.** The 2026-06-10 audit is a snapshot; containers are added
  and removed. Confirm against the live inventory before committing each stage's target — e.g.
  `docker compose -f /storage/docker/compose/docker-compose.yml config --services` cross-checked against
  `docker ps -a` and the table above.
- **Host-network apps** (library-organizer + others) are probed via the host IP, not a docker-network
  alias (per `reference_docker_inventory.md`).
- **`ghost-db` vs host DB:** `ghost-db` is a container MariaDB local to the ghost stack; do NOT confuse
  it with the host-native MariaDB/MySQL (EPIC-018-010).
- **This epic is breadth, not depth.** Each app gets liveness + dependency + log-error alerting, not a
  bespoke metrics integration. If an app later warrants a real integration bundle, promote it to its
  own epic (the EPIC-005 pattern) rather than expanding it here.
- **Excluded sets are documented above** specifically so a future session does not re-scope
  test-caddy / campaign / gm / EPIC-018 services into EPIC-021.

## Brainstorming session record

Scope + candidate list locked in this session (2026-06-10) from a live container audit cross-referenced
against the EPIC-018 deep-dive list and the EPIC-014 self-monitor set. The user confirmed exclusion of
the test-caddy and separate-project (campaign/gm) containers. Stage Design phases re-verify the live
inventory before targeting a container.
