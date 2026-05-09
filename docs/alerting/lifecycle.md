# Alert lifecycle

> Foundation epic (EPIC-001). Describes how an alert moves from a vmalert
> rule firing in VictoriaMetrics/VictoriaLogs through Alertmanager, into
> the monitor's own alert store, and onto the operator's screen — and
> how the operator acks or silences it.

## 1. Overview

In homelab-monitor, an alert is born when **vmalert** evaluates a rule
against **VictoriaMetrics** (metrics) or **VictoriaLogs** (logs) and
decides the rule is firing. vmalert pushes the firing notification to
**Alertmanager** (AM), which de-duplicates and groups alerts, then fans
them out to two consumers:

1. **Karma**, the read-mostly UI for AM. The operator views, silences,
   and acks alerts here.
2. The **monitor's own ingestor** (`POST /api/alerts/ingest`), which
   stores the alert in SQLite so the monitor can correlate it with its
   own state, dispatch follow-on actions, and (in later epics) drive
   auto-fix.

The operator interacts with alerts through the Alerts page in the
monitor UI, which embeds Karma in a sandboxed iframe. Acking an alert
is a Karma silence whose comment starts with `ACK!`. **kthxbye**, a
small Go daemon that watches Alertmanager, recognises the `ACK!`
prefix and keeps the silence alive (extending its `endsAt`) for as
long as the alert is firing. When the alert resolves, kthxbye stops
extending and the silence expires naturally.

For background, see the design spec:

- §3.2 — Sidecars (Alertmanager, Karma, kthxbye, vmalert ×2)
- §4.4 — Alert lifecycle (data flow)
- §8.4 — Notifications: lifecycle
- §9.1 / §9.2 — UI structure and per-screen specs

This document covers the foundation-epic behaviour. Right-side
enrichment drawer, run-fix button, and routing rule editor are
explicitly out of scope; see [§8](#8-out-of-scope-for-stage-001-019).

## 2. Components and their roles

```
                                +----------------------+
                                |  VictoriaMetrics     |
                                |  VictoriaLogs        |
                                +----------+-----------+
                                           |
                              evaluate rules (every 30s prod / 5s test)
                                           |
                          +----------------+----------------+
                          |                                 |
                +---------v----------+            +---------v----------+
                |  vmalert-metrics   |            |  vmalert-logs      |
                +---------+----------+            +---------+----------+
                          |                                 |
                          +---------------+-----------------+
                                          |
                                  POST firing/resolved
                                          |
                                +---------v----------+
                                |   Alertmanager     |
                                |   (dedupe, group,  |
                                |    route, silence) |
                                +----+----------+----+
                                     |          |
                       webhook to    |          |  HTTP API (read+write)
                  /api/alerts/ingest |          |
                                     v          v
                            +--------+----+   +-+------------+
                            |  monitor    |   |    Karma     |
                            |  (FastAPI)  |   |  (UI for AM) |
                            |  + SQLite   |   +-+------------+
                            +------+------+     |
                                   |            |
                                   |            +-- polls AM for silences
                                   |                with comment "ACK!*"
                                   |                       ^
                                   |                       |
                                   |                +------+--------+
                                   |                |   kthxbye     |
                                   |                |  (extends     |
                                   |                |   acks while  |
                                   |                |   alert fires)|
                                   |                +---------------+
                                   |
                                   v
                              browser  <--- iframe ---  reverse-proxy /api/karma/
```

### VictoriaMetrics (`victoriametrics`)

Time-series store for metrics scraped by `vmagent`. Listens on `:8428`.
Metric retention defaults to 90 days. vmalert-metrics queries it.

### VictoriaLogs (`victorialogs`)

Log store fed by `vector`. Listens on `:9428`. Log retention defaults to
30 days. vmalert-logs queries it. Not exposed outside the
`homelab-monitor-net` Docker network — operators reach it through the
monitor's `/api/logs/query` endpoint (auth required).

### vmalert (×2: `vmalert-metrics` and `vmalert-logs`)

Two instances of [vmalert][vmalert-docs]:

- `vmalert-metrics` evaluates `deploy/vmalert/metrics/*.yaml` against
  VictoriaMetrics on a 30-second interval (5s in the test rig).
- `vmalert-logs` evaluates `deploy/vmalert/logs/*.yaml` against
  VictoriaLogs on the same cadence.

Both push firing/resolved notifications to Alertmanager at
`http://alertmanager:9093`. Health is exposed at `/-/health`
(Prometheus convention).

[vmalert-docs]: https://docs.victoriametrics.com/vmalert/

### Alertmanager (`alertmanager`)

The standard Prometheus [Alertmanager][am-docs]. Responsibilities:

- **Dedupe** identical alerts received from vmalert.
- **Group** alerts by configurable labels so a single noisy condition
  doesn't fan out into a dozen notifications.
- **Route** to receivers per `alertmanager.yml`. The monitor's webhook
  receiver is `http://monitor:9090/api/alerts/ingest`, called with an
  API token bound to the `alerts:ingest:write` scope.
- **Silence** alerts on operator request (silences live in AM's own
  store, not the monitor's DB).

The config is rendered from
`deploy/alertmanager/alertmanager.yml.template` by the monitor at
startup and mounted into the AM container at `/etc/alertmanager/`.

[am-docs]: https://prometheus.io/docs/alerting/latest/alertmanager/

### Karma (`karma`)

[Karma][karma-docs] is a single-page web app that talks to
Alertmanager's API and presents alerts in a friendlier form than AM's
own minimal UI. It supports filtering, grouping, and creating /
deleting silences directly against AM.

Karma listens on port `8081` inside the `homelab-monitor-net` Docker
network. **It is not published to the LAN**; the only external path
in is the monitor's reverse proxy at `/api/karma/...` (see [§7](#7-the-proxy--iframe-architecture-security-note)).

Karma is configured with `listen.prefix: /api/karma/` so its asset and
API URLs match the path the monitor's reverse proxy serves them on.

[karma-docs]: https://github.com/prymitive/karma

### kthxbye (`kthxbye`)

[kthxbye][kthxbye-docs] is a small Go daemon that polls Alertmanager
on a tick and extends silences whose comment starts with a configured
prefix — for us, `ACK!`. It only extends a silence if:

1. The silence's comment starts with `ACK!`, AND
2. There is at least one alert still firing that matches the silence,
   AND
3. The silence's `endsAt` is within `extend-if-expiring-in` of "now".

Once the matching alert resolves (vmalert tells AM the rule no longer
fires), kthxbye stops extending. The silence then expires on its own
schedule.

The exact production tunables (from `deploy/compose/docker-compose.yml`):

```text
-extend-by=10m
-extend-if-expiring-in=6m
-interval=1m
-max-duration=24h
-extend-with-prefix=ACK!
```

See [§6](#6-auto-extend-timing-math-technical) for the timing math.

[kthxbye-docs]: https://github.com/prymitive/kthxbye

### monitor (this codebase)

The FastAPI process owns:

- **Ingest:** `POST /api/alerts/ingest` accepts Alertmanager v2
  webhook payloads (token-auth with `alerts:ingest:write` scope, or
  cookie session for operator-initiated dev pokes). Persists each
  alert in the SQLite `alerts` table, keyed by AM's stable
  fingerprint. Logs only counts and fingerprints at INFO; full
  payloads go to `alerts.payload_json` so the operator can inspect
  them via `GET /api/alerts/{id}`.
- **Read API:** `GET /api/alerts`, `GET /api/alerts/{id}`,
  `POST /api/alerts/{id}/ack`, `POST /api/alerts/{id}/dismiss` —
  session-only (no token).
- **Karma reverse proxy:** `/api/karma/{path:path}` → Karma's
  `/api/karma/{path}` after auth + Origin check. Implementation in
  `apps/monitor/homelab_monitor/kernel/api/routers/karma.py`.
- **Alerts UI route:** `/alerts` in the React SPA (see
  `apps/ui/src/routes/Alerts.tsx`). Embeds Karma in a sandboxed
  iframe.

## 3. Alert state machine

There is no enum named `AlertState` in code that captures all four
states below; rather, "state" is a derivation from
(vmalert-says-firing, AM-silenced, silence-comment-starts-with-`ACK!`).
Operationally, the four values to think about are:

| State    | Triggered by                                                       | Operator sees                                                  | Notifications? |
|----------|--------------------------------------------------------------------|----------------------------------------------------------------|----------------|
| Active   | vmalert evaluation says rule fires; AM receives `firing`           | Red row in Karma. Listed in `GET /api/alerts` with `firing`.   | Yes            |
| Silenced | Operator created a silence in Karma (or another client called AM)  | Greyed-out row in Karma; "muted" badge.                        | No             |
| Acked    | Silenced AND the silence comment starts with `ACK!`                | Greyed-out row with the operator's `ACK! ...` comment visible. | No             |
| Resolved | vmalert evaluation says rule no longer fires; AM marks `resolved`  | Disappears from Karma's default view; status `resolved` in DB. | "Resolved" notification per AM config |

A few important facts about the state machine:

- **`Active` and `Silenced` are not mutually exclusive in storage.**
  AM tracks "is a silence matching me right now?" as a flag on the
  alert, not as a status that replaces "firing". The alert is
  *firing AND silenced*; the silence merely suppresses notifications.
- **`Acked` is `Silenced` with a convention.** Nothing in
  Alertmanager itself treats `ACK!`-prefixed comments specially.
  kthxbye is what makes ack semantics real.
- **`Resolved` flows from vmalert.** The operator cannot "mark
  resolved" from the UI in the foundation epic; resolving requires the
  underlying condition to clear. (A "manually mark dismissed" action
  on the monitor's own `Alert` record exists via
  `POST /api/alerts/{id}/dismiss`, but that is a monitor-side note,
  not an AM state change.)

## 4. The ack vs silence distinction

This is the most important mental model in the foundation epic, and
it's worth spelling out plainly:

### Silence (default)

- **Time-bound.** The operator picks a duration (default 24 h) when
  creating the silence.
- **Suppresses notifications** matching the silence's matchers for
  the chosen window.
- **No memory of intent.** When the silence expires, AM resumes
  notifying. If the underlying alert is still firing, the operator
  gets notified again.
- **Use when:** "I'll deal with this in two hours, don't bother me
  before then."

### Ack (convention via `ACK!` comment prefix)

- **Open-ended in practice.** The operator still picks an initial
  duration, but kthxbye extends `endsAt` while the alert keeps
  firing.
- **Auto-expires when the alert resolves.** Once vmalert says the
  rule no longer fires, kthxbye stops extending; the silence then
  decays on its own.
- **Capped by `max-duration`.** kthxbye won't extend a single silence
  past 24 h total lifetime in production. If the alert is *still*
  firing after 24 h of acked silence, the operator gets re-notified
  — a forcing function against permanently-acked alerts.
- **Use when:** "I'm working on this; don't notify me again until
  I've actually fixed it."

Mechanically, an ack is just a silence where the comment happens to
start with `ACK!`. Karma and AM see no difference; only kthxbye does.

## 5. How to ack/snooze in the UI

1. Open the monitor in your browser (e.g. `https://monitor.example.com/`).
2. Click **Alerts** in the sidebar. The browser navigates to `/alerts`.
3. The page renders an iframe pointing at
   `/api/karma/`. Karma's UI loads inside it. (You're authenticated
   via the same monitor cookie session you used to log in — the
   reverse proxy sees that cookie and forwards the request.)
4. Find the alert you want to ack or snooze. Click the row.
5. Click the **Silence** button.
6. In the silence form:
   - **Comment:** start with `ACK!` if you want kthxbye to keep the
     silence alive while the alert fires (e.g.
     `ACK! looking into this`). Otherwise the silence is purely
     time-bound.
   - **Duration:** default 24 h is fine for most cases. The actual
     lifetime can extend up to `max-duration=24h` (the kthxbye cap)
     for `ACK!` silences.
   - **Matchers:** Karma pre-populates these from the alert. Leave
     them as-is unless you specifically want to broaden the silence.
7. Submit.

The silence is created against Alertmanager. Karma's view updates;
the alert row turns grey with the silence comment shown. Within the
next `interval=1m` poll, kthxbye picks up the silence and (if it
qualifies for `ACK!` extension) starts tracking it.

## 6. Auto-extend timing math (technical)

kthxbye's extension behaviour is fully determined by four flags on
its command line. From `deploy/compose/docker-compose.yml`:

| Flag                          | Production | Test rig |
|-------------------------------|------------|----------|
| `-interval`                   | `1m`       | `5s`     |
| `-extend-if-expiring-in`      | `6m`       | `15s`    |
| `-extend-by`                  | `10m`      | `30s`    |
| `-max-duration`               | `24h`      | `2m`     |
| `-extend-with-prefix`         | `ACK!`     | `ACK!`   |

The mechanics:

1. Every `interval`, kthxbye polls Alertmanager.
2. For each silence whose comment starts with `ACK!` and which has at
   least one currently-firing alert matching it: if `endsAt - now <=
   extend-if-expiring-in`, kthxbye sets the new `endsAt` to
   `endsAt + extend-by`.
3. If the cumulative lifetime of the silence (from `startsAt`) would
   exceed `max-duration`, the extension is capped — effectively no
   further extension is applied past 24 h.

**Net safety margin:**

- `extend-by` (10m) − `extend-if-expiring-in` (6m) = **4 minutes**

That's how much "headroom" the system has before a silence could
naturally expire while the alert is still firing. As long as kthxbye
wakes up every minute, it has at least four polls' worth of grace to
notice and extend. (If kthxbye is down for >4 minutes, an `ACK!`
silence may expire and the operator may get re-notified.)

**Why ordering matters:** kthxbye refuses to start if `extend-by <=
extend-if-expiring-in`, because the math would produce a negative
margin. The compose file's comments call this out explicitly.

**Why the test rig compresses the values:** wall-clock test cycles
like "create silence → wait for first extension → wait for `max-duration`
cap" need to complete in tens of seconds, not hours. The test rig's
`30s / 15s / 5s / 2m` keep the same ordering invariant
(`extend-by > extend-if-expiring-in`) so behaviour is faithful, just
faster.

## 7. The proxy + iframe architecture (security note)

Karma runs inside the `homelab-monitor-net` Docker network on port
`8081`. It is **not** published to the host LAN; the only ingress is
through the monitor's FastAPI app, which reverse-proxies
`/api/karma/{path:path}` to `http://karma:8081/api/karma/{path}`.

The proxy lives in
`apps/monitor/homelab_monitor/kernel/api/routers/karma.py`. Its
behaviour:

### Authentication

- **Cookie session required**, via the
  `require_session_no_csrf()` dependency. The iframe cannot mint a
  CSRF header (no access to the top frame's JS), so the proxy is
  CSRF-exempt at the dependency level.
- **API tokens are not accepted on this proxy.** Karma is an
  interactive-UI surface; programmatic AM callers go straight to AM
  on the internal network.

### CSRF compensation: Origin / Referer check

Because we bypass the global CSRF dependency, `_verify_origin()`
performs a **same-origin check** on every state-changing request
(POST/PUT/PATCH/DELETE):

- The "expected" origin is built from
  `request.headers["host"]` (or `request.url.netloc`) plus the
  scheme.
- The scheme is derived from `request.url.scheme` by default, **OR**
  from the `X-Forwarded-Proto` request header if the env var
  `HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS=1` is set.
- The request's `Origin` header (preferred) or `Referer` header
  (fallback) MUST match the expected origin. Missing both → 403.
  Mismatch → 403.

For deployments behind nginx (the standard production setup), set
`HOMELAB_MONITOR_TRUST_FORWARDED_HEADERS=1` so the monitor sees the
external scheme as `https` instead of the internal `http` between
nginx and the container. nginx itself MUST be configured to
*generate* the `X-Forwarded-Proto` from server-side state, not
forward a client-supplied one.

### Header allow-listing

The proxy strips Cookie and Authorization on the way upstream (those
are the monitor's, not Karma's), and strips Set-Cookie /
X-Frame-Options / Strict-Transport-Security on the way back (defence
in depth). Only an explicit allow-list of headers crosses the proxy
in either direction; everything else is dropped.

### Path validation

The captured path is checked against a regex (`[A-Za-z0-9._/~+\-%]*`),
rejected if it contains `..` segments, and rejected if it contains a
null byte. Querystrings are forwarded as-is (FastAPI captures them
separately and they never reach the path validator).

### Iframe sandbox

The Alerts route at `/alerts` (see `apps/ui/src/routes/Alerts.tsx`)
embeds Karma in an iframe with:

```html
<iframe sandbox="allow-scripts allow-same-origin allow-forms" ...>
```

- `allow-scripts` — Karma is an SPA; it doesn't load without JS.
- `allow-same-origin` — needed so the iframe's XHRs to
  `/api/karma/...` carry the monitor's session cookie.
- `allow-forms` — Karma's silence-creation flow uses form
  submissions in some flows.

The trade-off here (called out in the route's JSDoc) is that
`allow-same-origin` largely cancels other sandbox restrictions —
since the iframe IS same-origin with the parent. The real isolation
comes from the proxy's auth + Origin check, not the sandbox attribute.

## 8. Out of scope for STAGE-001-019

The following are **deliberately not built** in the foundation epic
and are flagged here so the operator knows where to expect them:

### Right-side enrichment drawer

A panel that, on alert click, shows the monitor's own correlated data
(recent log lines, related runbook history, the monitor's
`AlertOutcome` for that alert ID). **Deferred.** The blocker is
purely a UX-binding problem: Karma has no per-alert deep-link nor a
postMessage API the parent frame can listen to, so "click an alert in
the iframe → open a drawer in the parent" isn't implementable as a
click pattern. The path forward is a separate
`useAlertOutcome(alertId)` lookup keyed by something other than an
iframe click event — likely a dual list (Karma in iframe + monitor's
own list rendered alongside). Tracked for a future UI stage.

### Run-fix button (auto-remediation trigger)

A "fix this now" button that triggers an allow-listed runbook via
the auto-fix subsystem. **Deferred to EPIC-009 (auto-fix).** The
infrastructure for safe runbook execution
(`homelab-fixer` low-priv user, per-runbook rate-limit + cooldown,
dry-run + ack gating, kill switch) is the EPIC-009 deliverable; the
button is the UI surface on top of it. See `project_autofix_safety_model.md`.

### Routing rule editor

A UI for editing AM's routing tree (which alerts go to which
notification channels at which severities). **Deferred to EPIC-012.**
For now, route changes are made by editing
`deploy/alertmanager/alertmanager.yml.template` and reloading AM via
its `/-/reload` endpoint.

---

For the source-of-truth data flow narrative, see
`docs/superpowers/specs/2026-05-04-homelab-monitor-design.md` §4.4
and §8.4.
