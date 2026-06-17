# EPIC-022: Public web reachability + TLS/certbot + AWS (Route 53, domains, spend)

## Status: Not Started (placeholder)

## Overview

Consolidate everything about the homelab's **public-facing web presence and its AWS/TLS backing**
into one integration-flavoured epic. This was carved out during the 2026-06-16 Pi-hole brainstorm
because the pieces below were either missing entirely or scattered thinly across EPIC-014 (cert
expiry), EPIC-016 (WAN/cert-renewal-status), and EPIC-018 (nginx-configuator inventory enrichment).
None of those owns end-to-end **"is each public subdomain actually serving"**, **Route 53 health**,
**domain-registration expiry**, or **AWS cost** — this epic does.

The user explicitly requested (2026-06-16):
- Alerting if **any subdomain in `/storage/programs/nginx-configuator/sites-config.yaml`** is not reachable.
- **certbot certificate management + monitoring** (renewal-cron health, not just expiry).
- **AWS Route 53 monitoring** (hosted-zone / health-check status).
- **AWS domain monitoring** (registrar / domain-registration expiry for the user's domains).
- **AWS spend / cost monitoring** (this one is genuinely new — not in the master spec anywhere).

## Five pillars (decompose into stages during this epic's Design phase)

1. **Per-subdomain reachability probes.** Read `sites-config.yaml` (read-only) — currently ~22 sites
   (`podcast/dw/jakekausler.com/plex/udo/kingmaker/foundry/library/deadlands/bills/grocy/billsdev/
   frigate/teacherinsights/languagetutor/campaign/blog.jakekausler.com`, etc.). For each enabled site,
   probe the **public** HTTPS endpoint (full path through nginx → upstream), assert a healthy response
   (status code, optionally a per-site expected-string / health-route like campaign's `/health`),
   measure latency, and emit `homelab_site_up{host}` + `homelab_site_response_seconds{host}`. Per-site
   warn/crit + a roll-up "N sites down" alert. The probe set is **config-driven from `sites-config.yaml`**
   (auto-discovers new sites), NOT hardcoded. Some sites have WS upstreams (campaign/foundry/etc.) — the
   probe targets the HTTP health surface, not the websocket, unless a per-site override says otherwise.
   **Resolver rule:** these reachability probes MUST resolve via a direct upstream (1.1.1.1/8.8.8.8),
   NOT Pi-hole — same circular-dependency rule as EPIC-006/014/016.
2. **certbot / Let's Encrypt management + monitoring.** Beyond EPIC-014's cert-EXPIRY walk: monitor the
   **renewal mechanism itself** — `nginx-configuator` runs certbot via the user's existing daily 12:00
   cron; track last-successful-renewal age, renewal failures (parse certbot output / the cron's
   heartbeat), and surface per-domain "renewal is failing" before the cert actually expires. Cooperates
   with `nginx-configuator` (does NOT replace it). Cross-references EPIC-014 STAGE-014-001/002 (expiry +
   served-cert reachability) — this epic adds the renewal-health layer on top.
3. **AWS Route 53 monitoring.** Hosted-zone record health + any configured Route 53 **health checks**
   (status, recent failures). The apex `jakekausler.com` A record is updated by the existing `ip-update`
   container (EPIC-018 STAGE-018-012 tracks that container's last-update timestamp; this epic adds the
   Route-53-SIDE confirmation that the record actually holds the expected value + health-check status).
   Needs a read-only AWS IAM credential (Route 53 read).
4. **AWS domain (registrar) monitoring.** Domain-registration expiry + auto-renew status via the Route 53
   Domains API (distinct from cert expiry — this is the DOMAIN itself lapsing). Alert well ahead (e.g.
   60/30/14 days). Read-only IAM.
5. **AWS spend / cost monitoring.** Poll the AWS Cost Explorer API (read-only `ce:GetCostAndUsage`):
   month-to-date spend, per-service breakdown, forecast, and an anomaly/threshold alert ("spend > $X this
   month" / "daily spend spiked"). Genuinely new capability — not in the master spec. Needs a read-only
   Cost Explorer IAM credential. Note Cost Explorer API calls cost ~$0.01 each — poll on a low cadence
   (e.g. a few times/day), not every minute.

## Architecture notes (for the Design phase)

- **AWS credentials:** a single read-only IAM principal (or one per service) stored in the secrets store
  (`aws_access_key_id` / `aws_secret_access_key` / region), scoped to: `route53:Get*/List*`,
  `route53domains:Get*/List*`, `ce:GetCostAndUsage`/`GetCostForecast`. Open-source-safe: ships disabled;
  the user's override repo supplies the credential. Document the minimal IAM policy in the epic's README.
- **Probe target source of truth** = `sites-config.yaml` (read-only mount, same file EPIC-018 reads for
  inventory). A discoverer can suggest "new public site X appeared in sites-config" → suggestion queue.
- **UI:** a dedicated Integrations → "Web / TLS / AWS" panel (or fold into a "Public presence" area):
  per-site reachability grid, cert/renewal table, Route 53 record/health table, domain-expiry roundup,
  AWS spend chart (MTD + forecast + per-service). Embedded `<LogViewer>` for certbot/ip-update logs.
- **Grafana:** a `web-tls-aws.json` dashboard (site uptime heatmap, cert-days-remaining, AWS spend trend).
- **Digest:** contributes a "public presence + AWS spend" digest section (EPIC-013).

## Dependencies

- EPIC-001 (foundation), EPIC-004 (LogViewer embed contract + log rules for certbot/ip-update output).
- EPIC-014 (cert expiry + served-cert reachability — this epic builds the renewal-health + AWS layers ON
  TOP; do NOT duplicate the `/etc/letsencrypt/live/*` walk).
- EPIC-016 (WAN/DNS health — the public probes assume WAN is the dependency; cross-reference for "is it
  the site or the whole WAN" disambiguation).
- EPIC-018 STAGE-018-012/013 (ip-update health + sites-config inventory enrichment — this epic consumes
  those targets, adds active probing + Route-53-side confirmation).

## Cross-stage acceptance criteria

Same as EPIC-001 plus:
- **All reachability/DNS probes resolve via direct upstream, never Pi-hole** (circular-dependency rule).
- **AWS read-only only.** No write/mutate AWS calls. Credentials least-privilege, in secrets store, ships
  disabled in the public release.
- **Cost Explorer cadence bounded** (API calls are billed) — low-frequency polling, cached.
- **Domain/IP/spend data is mildly sensitive** — not logged at info by default; configurable to verbose.

## Notes

- This epic is the consolidated home the user asked for so the nginx/cert/AWS asks "aren't lost." Created
  2026-06-16 during the Pi-hole (EPIC-006) brainstorm. Full stage decomposition happens when this epic is
  begun (it is late in the roadmap — after the core integrations).
- Cross-reference breadcrumbs were ADDED to EPIC-014, EPIC-016, and EPIC-018 pointing here, so the scattered
  cert/Route53/nginx mentions there now explicitly defer the reachability-probe / Route53-health / domain /
  spend work to EPIC-022.
- AWS-spend monitoring is the one item with no master-spec precedent; flag during Design whether it warrants
  its own sub-area or folds into the AWS panel.
