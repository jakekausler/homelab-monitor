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

## Dependencies

- EPIC-001 (VM, vmalert, AM, Karma).
- EPIC-010 (tool-effectiveness analyzer) — ideally landed before EPIC-015 so the shadow-rule data is consumed immediately. If EPIC-010 isn't done yet, the data is collected anyway and consumed later.

## Notes

- The user-facing reframe of Netdata: "this is the second opinion on whether your hosts are misbehaving." It complements thresholds rather than replacing them.
- Netdata Cloud is optional and not part of this epic — fully self-hosted operation.
- Netdata's k-means model trains over the first ~6 hours after install. The user should expect noisy alerts during that window; the dashboard surfaces a "Netdata is still calibrating" banner.
