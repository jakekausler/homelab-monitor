# `components/logs` — shared log-display primitives

Three building blocks plus one convenience default. Used by the docker
container log viewer, the cron run log viewer, the Logs Explorer
(STAGE-004-010), and future inventory detail pages (HA / Pi-hole / Unifi /
Synology — EPIC-005/006/007/008) per master spec §9.2 ("inventory detail
pages include related logs").

## Primitives

- **`LogLineList`** — the `<pre>` line list. One `<div>` per line:
  `<span>{timestamp}</span> {message}`, severity text-tint, horizontal
  scroll on narrow viewports. Props: `lines: LogLine[]`, `emptyContent?`,
  `testId?`, `wrap?` (boolean, default false — toggles soft-wrap vs horizontal scroll).
- **`LogBanner`** — small rounded notice box. Props: `tone: 'amber' | 'blue'`,
  `testId?`, `role?: 'status' | 'alert'`, `children`.
- **`LogViewer`** — convenience default composing the primitives in a stacked
  layout. Data-source agnostic: the caller supplies `useLogs()`. This is the
  drop-in for detail pages and the Explorer.

## Embedding contract

`LogViewer` is data-source agnostic. The caller owns the data hook and adapts
its response into `UseLogsResult` (`lines`, `isLoading`, `isError`, `error`,
`logStatus`, `truncated`). Map your endpoint's raw status to the normalized
`logStatus` enum: `'available' | 'no_lines' | 'unavailable' | 'unknown' |
'expired' | 'running'`. Put any source-specific controls (time-range picker,
refresh button) in `headerSlot`.

## Example: embed related logs on a detail page

```tsx
import { LogViewer } from '@/components/logs/LogViewer'
import type { UseLogsResult } from '@/components/logs/types'
import { useLogsQuery } from '@/api/logs'

function HostRelatedLogs({ host }: { host: string }) {
  const useLogs = (): UseLogsResult => {
    const q = useLogsQuery({ filter: `host:${host}`, range: '1h' })
    return {
      lines: q.data?.lines,
      isLoading: q.isLoading,
      isError: q.isError,
      error: q.error ?? undefined,
      logStatus: q.data ? (q.data.lines.length ? 'available' : 'no_lines') : undefined,
      truncated: q.data?.truncated,
    }
  }
  return <LogViewer useLogs={useLogs} emptyStateCopy="No recent logs for this host." />
}
```

## When NOT to use `LogViewer`

If your surface needs a structurally different shell (e.g. the cron run viewer
renders loading/error inside a sticky metadata header), compose `LogLineList`
and `LogBanner` directly instead of `LogViewer`.

## Deep-linking to the Explorer

`OpenInExplorerButton` SPA-navigates (TanStack `<Link>`, no full reload) to the
`/logs` Explorer with pre-filled filters and time range. It wraps the pure
helper `buildExplorerUrl` (`@/lib/explorerLink`), which mirrors the Explorer's
own URL serialization (`LogsExplorerPage.writeUrl`).

Use it on any surface that shows a scoped slice of logs (a container, a cron
run, and — in EPIC-005/006/007/008 — HA / Pi-hole / Synology / Unifi detail
pages) to let the user jump to the full Explorer pre-scoped to that slice.

The button takes the same options as the helper: `logsQl` (advanced LogsQL,
wins over `plainText`), `plainText`, `selectedServices` (pre-formatted
`source_type:service` strings), and a time range — either `sincePreset` (a
preset token, wins over the explicit range) or `rangeStart`/`rangeEnd` Dates
(start without end is allowed = open-ended).

### Docker container log viewer

```tsx
// Use fieldFilterClause from @/lib/logsQlTranslate for safe escaping:
<OpenInExplorerButton
  logsQl={fieldFilterClause('service', containerName)!}
  sincePreset="15m" // or rangeStart/rangeEnd in custom mode
/>
```

### Cron run log viewer

```tsx
// Use fieldFilterClause from @/lib/logsQlTranslate for safe escaping:
<OpenInExplorerButton
  logsQl={`${fieldFilterClause('cron_fingerprint', fingerprint)!} AND ${fieldFilterClause('run_id', runId)!}`}
  rangeStart={new Date(runMin.getTime() - 1000)}
  rangeEnd={new Date(runMax.getTime() + 1000)}
/>
```

Future inventory detail pages reuse this same component and helper.
