# EPIC-016: ISP / WAN collectors

## Status: Not Started

## Overview

Build the dedicated ISP/WAN-side monitoring per spec §2 Q21. All listed signals in scope: WAN reachability, external IP tracking (integrating with the existing `ip-update` Docker container), latency/jitter, multi-hop packet loss (mtr-style), speedtest (reuse UDM-side speedtest results), DNS resolution health, modem health (placeholder until the user shares the AT&T modem model), CGNAT/inbound reachability, and cert renewal status (latter overlaps with EPIC-014's cert work).

## Source documents

- Spec §2 Q21 (all signals in scope), §3.4 (discovered targets includes external endpoints), §16 (`nginx-configuator` and Route 53 context).

## Stages (to decompose during epic Design phase)

| Likely stage | Theme |
|---|---|
| STAGE-016-001 | WAN reachability collector: ping (ICMP) and HTTP probe of well-known endpoints (1.1.1.1, 8.8.8.8, google.com, cloudflare.com); separate metrics per endpoint |
| STAGE-016-002 | External IP tracking: a small collector that queries `https://api.ipify.org` (or similar, configurable) on a 5min cadence; emits `homelab_external_ip{ip}` with high cardinality only when changes occur (use a counter incrementing on change to avoid label explosion); cross-references the existing `ip-update` container's last known IP |
| STAGE-016-003 | Latency + jitter: ongoing 1-minute moving window of pings to 1.1.1.1, 8.8.8.8; emits latency p50/p95/p99 + stddev (jitter); rule on sustained spikes |
| STAGE-016-004 | Packet loss + multi-hop trace: periodic `mtr` runs (subprocess plugin since `mtr` is a binary, not a Python lib); emits per-hop loss; rule when sustained packet loss > 1% on the WAN-facing hops |
| STAGE-016-005 | DNS resolution health: dual-path resolution test — resolve a known domain via Pi-hole and via 1.1.1.1 directly; emit `homelab_dns_resolution_seconds{path}` so Pi-hole vs WAN issues are distinguishable |
| STAGE-016-006 | UDM speedtest result surfacing: when EPIC-007 lands the unifi-poller, speedtest results are already in VM. This stage adds rules and dashboard panels for them |
| STAGE-016-007 | CGNAT / inbound reachability: an external service (or a small relay we run on a VPS) attempts to reach our public port; emits `homelab_inbound_reachable_bool` |
| STAGE-016-008 | Modem health collector: stubbed plugin interface. The actual scraper is deferred until the user provides the AT&T modem model. The plugin contract and a TODO live here |
| STAGE-016-009 | Default Grafana dashboard `isp-wan.json` and default vmalert rules |

## Cross-stage acceptance criteria

Same as EPIC-001 plus:

- **DNS health checks NEVER use Pi-hole when checking WAN DNS.** Same circular-dependency rule as EPIC-006.
- **Outbound bandwidth from the monitor itself bounded.** No collector should burn meaningful bandwidth (mtr probes are small; speedtest is only surfaced from UDM's own runs, not initiated by us).
- **External-IP tracking does not log the IP at info level by default** — IP is mildly sensitive PII. Configurable to verbose.

## Dependencies

- EPIC-001.
- EPIC-007 (Unifi) — UDM speedtest surfacing depends on the unifi-poller.
- EPIC-014 (cert reachability collector overlaps; this epic doesn't duplicate it).

## Notes

- The CGNAT-reachability check is the trickiest; ideally we have a tiny VPS or we use a free public ping service that supports inbound checks. Stage 007's Design phase chooses.
- AT&T modem scraping is a known-unknown; many AT&T modems have a basic web UI at `192.168.1.254` with line stats; STAGE-016-008 documents the plugin interface and waits for the user.
- **Scope boundary with EPIC-022 (added 2026-06-16):** the "cert renewal status" mentioned in this epic's Overview is now owned by **EPIC-022 (web/TLS/AWS)** — specifically certbot renewal-health, per-subdomain public reachability probes (`nginx-configuator/sites-config.yaml`), **AWS Route 53 health**, **domain-registration expiry**, and **AWS spend**. This epic keeps the WAN-side signals (reachability, latency, jitter, packet-loss, external-IP, the Pi-hole-vs-1.1.1.1 DNS split in STAGE-016-005, CGNAT, modem). The Route-53 health-check status that spec §16 hinted at lives in EPIC-022, not here.
