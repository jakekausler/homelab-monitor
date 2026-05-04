# EPIC-003: Docker collector + per-container probes + label-based discovery + diun-style updates

## Status: Not Started

## Overview

Deliver Docker-aware monitoring. Build a collector that reads the Docker socket for container-level metrics (running/exited/restart count/exit codes/healthcheck status), wires up cadvisor for resource metrics, drives label-based per-container probe configuration, and adds image-update detection (diun-style digest comparison) plus a dashboard "Pull & Restart" action per container. Notifications-only by default; auto-update is OFF.

This epic is the first to land *real* per-container monitoring on top of EPIC-001's foundation. It is also the first epic that exercises the discovery engine and the label-driven config pattern from spec §2 Q13 (A3 — labels primary, config override).

## Source documents (read before starting any stage)

- Spec §3.4 (discovered targets includes "All Docker containers"), §5.5 (plugin discovery with labels), §11 (compose), Q13 (label-based config).
- Project memory `reference_docker_inventory.md` — the full container list. Pay attention to:
  - Containers with `network_mode: host` (HA, Plex, Pi-hole, library-organizer, matter-server, music-assistant) — probe via host IP, not container IP.
  - Locally-built images (`build: context: ...`) — diun digest comparison doesn't apply; use a different update-detection strategy (file checksum / source-repo mtime).
  - Containers depending on `/rackstation/*` mounts (Plex, Frigate, podcast-feed) — mount-health probe is upstream of these.
  - Host-native MariaDB and MySQL — out of scope here (covered in EPIC-018 service deep-dives).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-003-001 | Docker socket collector: container inventory, status, restart counts, exit codes, healthcheck status, basic resource metrics via Docker stats API |
| STAGE-003-002 | cadvisor sidecar + scrape config update; richer resource metrics (CPU/RAM/IO/network per container/per cgroup); replaces overlapping pieces of 001 |
| STAGE-003-003 | Docker discoverer: socket events emit suggestions for new containers; user accepts to create per-container probes |
| STAGE-003-004 | Label-based probe config: containers with `homelab-monitor.http=...`, `homelab-monitor.metrics=...`, `homelab-monitor.tcp=port:host`, `homelab-monitor.exec=cmd` are auto-probed; per-service config file in `homelab-monitor.yaml` overrides labels |
| STAGE-003-005 | Image-update detection (diun-style): periodic digest comparison against the image's registry; metric `homelab_image_update_available{container,current_digest,latest_digest}`; vmalert rule emits per-container alert at severity=info |
| STAGE-003-006 | Update-detection for locally-built images: hash the build context's source tree; alert when source has changed but image hasn't been rebuilt |
| STAGE-003-007 | "Pull & Restart" dashboard action: per-container button; orchestrates `docker compose pull && up -d <service>` (calling the user's compose file path, configurable); audit log; confirm-on-destructive |
| STAGE-003-008 | Per-integration drill-down panel "Docker" in Integrations sidebar: container grid with status, image-update badge, last-restart, healthcheck status, action buttons |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **The user's compose file is read-only** by every collector and probe; only the explicit "Pull & Restart" action invokes `docker compose` write operations, and only with confirmation.
- **Probe failure does not cause container restart.** A misbehaving probe alerts; the user decides if action is needed.
- **Label collisions are detected** and surfaced as suggestions, not silently dropped.

## Dependencies

- EPIC-001 (kernel, API, alerts, dashboard).
- EPIC-002 (heartbeat receiver — used for per-container "synthetic heartbeat" if a container should be calling out periodically).
- The discoverer plugin kind is exercised live for the first time here — small refinements to the plugin contract from STAGE-001-006 may surface.

## Notes

- The compose file lives at `/storage/docker/compose/docker-compose.yml` on the user's host. The path is configurable; the public release does not assume this path.
- `host.docker.internal: host-gateway` is used by several user containers — probes that resolve container hostnames must handle this.
- The default probe set per discovered container is conservative: HTTP probe on declared label only, no `exec` probes by default (those require explicit user opt-in for safety).
- Disabled containers (`profiles: ["disabled"]`) are listed but not probed; the discoverer surfaces them as informational suggestions ("Frigate exists but is disabled — probe when enabled?").
