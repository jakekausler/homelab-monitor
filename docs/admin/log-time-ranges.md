# Log Time Ranges

The time-range control appears on every log viewer and scopes the query to a
recent window or a specific historical interval.

## Presets

Six quick-select presets are available: **5m**, **15m**, **1h**, **6h**,
**24h**, and **7d**. Each resolves to a window ending at the current moment;
the Refresh button keeps it anchored to "now" as time passes.

Active preset is encoded in the URL as `?since=<token>` (e.g. `?since=1h`).

## Custom range

Open the control, select **Custom range**, fill in a start and/or end datetime
(browser-local time), and click **Apply**. Both fields are optional:

| Start | End | Resolves to |
| --- | --- | --- |
| provided | provided | exactly that window |
| empty | provided | 30 days before the provided end |
| provided | empty | provided start → now |
| empty | empty | 30 days ago → now |

On the **cron run-log viewer** bounds are clamped to the run's own window — an
open start resolves to the run-start (not 30 days ago), and you cannot request
logs outside that run's interval.

A custom range is reflected in the URL as `?start=<ISO>&end=<ISO>` for
shareable, bookmarkable links.

## Validation rules

The UI validates before sending and shows an inline error on violation. The
backend enforces the same rules and returns HTTP `400` on any violation.

| Rule | Error |
| --- | --- |
| Start must be before end | "Start must be before end." |
| Neither bound in the future | "Times cannot be in the future." |
| Span ≤ 30 days | "Range cannot exceed 30 days." |

The 30-day cap matches VictoriaLogs retention. Querying beyond that window is
accepted but returns nothing — VL has no data older than its retention horizon.

## Where the control appears

- **Docker container logs** — open bounds resolve as above, no window constraints.
- **Cron run logs** — bounds clamped to the run's time window.
- **Logs Explorer** — forthcoming (STAGE-004-010); same control and URL conventions.
