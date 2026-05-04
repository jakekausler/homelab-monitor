# EPIC-006: Pi-hole integration

## Status: Not Started

## Overview

Land Pi-hole as a plugin bundle: a collector for query stats / blocked-domain stats / upstream resolver health, the per-integration dashboard panel, default Grafana dashboard, and default vmalert rules. Pi-hole is one of the most important services in this homelab — when it dies, everything on the LAN loses DNS resolution.

## Source documents

- Spec §3.4 (discovered targets), §6.2 (`pihole-exporter` style metrics), §15+ (per-service integrations are plugin bundles).
- Project memory `reference_docker_inventory.md`: the user runs `pihole-unbound` (image `mpgirro/pihole-unbound`) at `network_mode: host`, port 8080, password stored in compose env (`FTLCONF_webserver_api_password`). DNSSEC is enabled. Logs go to `/storage/docker/pihole-unbound/logs`.

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-006-001 | Pi-hole API client + secret-store key (`pihole_api_password`); smoke connectivity check (the API auth is now password-based since Pi-hole v6) |
| STAGE-006-002 | Stats collector: queries today, blocked today, percent blocked, top-clients (anonymized for privacy), top-blocked, upstream queries; emits `homelab_pihole_*` metrics |
| STAGE-006-003 | Upstream resolver health: ping each upstream resolver Pi-hole forwards to (configured in Pi-hole); detect upstream slow / failing |
| STAGE-006-004 | Unbound health (since the user's image bundles Unbound): cache hits, root server reachability, DNSSEC validation rate |
| STAGE-006-005 | DNS-resolution health collector (cross-cuts EPIC-016): resolves a known domain via Pi-hole AND directly via 1.1.1.1 to distinguish Pi-hole-broken vs WAN-broken |
| STAGE-006-006 | Pi-hole integration UI panel; default Grafana dashboard `pihole.json`; default vmalert rules (`PiholeQueriesStalled`, `UpstreamFailing`, `BlockedRateAnomaly`) |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **DNS test must NOT use Pi-hole when checking "is Pi-hole alive"** — circular dependency would mask outages. Tests must use direct upstream resolvers.
- **No client-IP-level data emitted to VM by default.** Privacy-preserving aggregates only (top-N counts without IPs); explicit opt-in via config to include client IPs.

## Dependencies

- EPIC-001.
- EPIC-005 not strictly required (HA push channel) but routing critical Pi-hole alerts to HA push is the expected configuration.

## Notes

- The user's Pi-hole password is currently in plaintext in `/storage/docker/compose/docker-compose.yml`. The integration's setup docs in this epic recommend rotating it, storing the new password in our secrets store, and updating the compose. (Out of scope for the integration itself — but worth a Note line in the README.)
- Pi-hole's API changed substantially in v6 (sessions/auth model). Verify the current version on the user's deployment during STAGE-006-001 Design phase.
