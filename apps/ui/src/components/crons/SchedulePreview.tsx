import { usePreviewExpr, usePreviewSavedCron } from '@/api/crons'
import { formatAbsolute } from '@/lib/relativeTime'

interface BaseProps {
  count?: number
}

export function SchedulePreviewForExpr({ expr, count = 3 }: BaseProps & { expr: string }) {
  const trimmed = expr.trim()
  const query = usePreviewExpr(trimmed, count, trimmed.length > 0)

  if (trimmed.length === 0) {
    return <p className="text-sm text-muted-foreground">Enter a schedule to preview next runs.</p>
  }
  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Calculating…</p>
  }
  if (query.error) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {query.error.message}
      </p>
    )
  }
  return <PreviewList runs={query.data?.runs ?? []} />
}

export function SchedulePreviewForSaved({ cronId, count = 3 }: BaseProps & { cronId: string }) {
  const query = usePreviewSavedCron(cronId, count, true)

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Calculating…</p>
  }
  if (query.error) {
    return (
      <p className="text-sm text-muted-foreground">
        Schedule preview unavailable ({query.error.message}).
      </p>
    )
  }
  return <PreviewList runs={query.data?.runs ?? []} />
}

function PreviewList({ runs }: { runs: string[] }) {
  if (runs.length === 0) {
    return <p className="text-sm text-muted-foreground">No upcoming runs.</p>
  }
  return (
    <ul className="space-y-1 text-sm">
      {runs.map((run) => (
        <li key={run} className="font-mono text-xs text-muted-foreground">
          {formatAbsolute(run)}
        </li>
      ))}
    </ul>
  )
}
