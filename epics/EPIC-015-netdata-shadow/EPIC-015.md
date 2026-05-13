# EPIC-015: Netdata + comparative shadow rules

## Status: Not Started

## Overview

Add Netdata to the stack as the dedicated behavioral-anomaly-detection layer. The user's stack already has VictoriaMetrics-based threshold rules; Netdata adds 18-model k-means anomaly detection at the edge that can catch behavioral outliers thresholds miss.

Per spec §2 Q16 (option A) and §2 Q17 (option D), Netdata's value vs. cost will be evaluated by the tool-effectiveness analyzer over time (EPIC-010); this epic stands it up and runs the first comparative shadow rules so the analyzer has data to work with.

## Source documents

- Spec §3.2 (Netdata sidecar — runs as Docker container with `/proc`, `/sys`, `/etc/os-release`, `/var/run/docker.sock` mounted read-only), §3.4 (discovered targets), §6.2 (Netdata-streamed metrics under `_netdata_*` namespace), §10.3 (resource budget).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-015-001 | Netdata sidecar in compose: pinned image, mounts, env config; remote-write to VM via Prometheus protocol |
| STAGE-015-002 | Netdata UI access: Netdata's own UI (port 19999) reverse-proxied through our auth at `/netdata/*` |
| STAGE-015-003 | First Netdata anomaly subscription: the host CPU metric set; consume Netdata's anomaly bit via its API/MetricsQL once streamed to VM; default vmalert rule fires when anomaly bit is set for sustained periods |
| STAGE-015-004 | Comparative shadow rule: vmalert rolling-baseline rule on the same CPU metric set runs in parallel with the Netdata anomaly subscription. Both produce alerts tagged with distinct `source_tool` so EPIC-010's tool-effectiveness analyzer can compare them |
| STAGE-015-005 | Default Grafana dashboard `netdata.json` showing Netdata's own panels via VM datasource |
| STAGE-015-006 | Future-extensibility: docs for adding Netdata agents on other hosts (Synology, future hosts) and pointing them at this VM as their remote-write destination |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **Netdata's own UI is auth-gated** through our reverse proxy; never exposed directly to the LAN.
- **Resource budget enforced.** Netdata is the heaviest sidecar in our stack (~200-500 MB); if its measured RSS exceeds the configured cap, an alert fires and we surface a recommendation in Tool Analysis to reduce its scope.
- **The shadow-rule pair is the integration test.** EPIC-001's canonical e2e tests do not change; this epic adds shadow-rule-pair-tested alerts to the rig.
- **STAGE-002-006 cross-epic criterion (added 2026-05-12):** When agent-push cross-host cron discovery ships in this epic, the `source_path IS NULL` remote-cron banner in `apps/ui/src/routes/inventory/CronDetail.tsx` (introduced in STAGE-002-006) MUST be removed (or updated) for newly-discovered remote crons whose `source_path` becomes populated via the new discovery mechanism. The banner copy currently reads: "Remote cron on `<host>`. The monitor doesn't have direct file access to this host. Wrapper-based heartbeats are the only signal." Update the trigger condition (the banner shows only when `source_path IS NULL`) so the banner stops showing for newly-enrolled hosts whose disk-source files become readable via the agent push.

## Dependencies

- EPIC-001 (VM, vmalert, AM, Karma).
- EPIC-010 (tool-effectiveness analyzer) — ideally landed before EPIC-015 so the shadow-rule data is consumed immediately. If EPIC-010 isn't done yet, the data is collected anyway and consumed later.

## Notes

- The user-facing reframe of Netdata: "this is the second opinion on whether your hosts are misbehaving." It complements thresholds rather than replacing them.
- Netdata Cloud is optional and not part of this epic — fully self-hosted operation.
- Netdata's k-means model trains over the first ~6 hours after install. The user should expect noisy alerts during that window; the dashboard surfaces a "Netdata is still calibrating" banner.

## Cross-epic absorbed scope (from EPIC-002 cron derived-state redesign, 2026-05-11)

Per `docs/superpowers/specs/2026-05-11-cron-derived-state-redesign.md`, this epic absorbs the **agent-push cross-host work** for the cron monitoring subsystem:

- **`hm-agent` binary** — a small program that runs on a remote host (e.g., Synology, NAS, foreign-network containers) as a recurring cron (or systemd timer) and POSTs:
  1. Netdata stream (existing scope of EPIC-015).
  2. **Cron discovery manifest** — `POST /api/discovery/report` with `{host, source_path, schedule, command}` entries from local crontab scans. The monitor ingests these and writes fingerprint-keyed rows into the `crons` table (audit verb `crons.discover`), populating `source_path` from the agent's claim.
  3. Other future resource discoveries (containers, services).
- **Host registration / enrollment flow** — per-host enrollment tokens, `hm agent enroll <token>` on the target host, agent receives a long-lived per-host API token on first contact, subsequent reports authenticate with that token. EPIC-015 owns this flow because it's the natural home for "I want this remote host to report into my monitor."
- **`/api/discovery/report` endpoint** — accepts manifests from enrolled hosts; verifies the enrollment token; writes/upserts rows. Separate from the local `cron-discoverer` plugin (which only scans the monitor's own host).

These pieces are NOT in EPIC-002. EPIC-002 ships only local-host cron discovery + local-host wrapper install. Anything cross-host is the agent's job.

Suggested decomposition: add a new stage (e.g., STAGE-015-007: agent binary + enrollment + cron discovery report) to this epic, after the Netdata-streaming stages.
