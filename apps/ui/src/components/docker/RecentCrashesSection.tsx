import { useState } from 'react'
import type { JSX } from 'react'

import { useContainerCrashes, useContainerCrashDetail } from '@/api/docker'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { LogViewer } from '@/components/logs/LogViewer'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import type { LogLine, UseLogsResult } from '@/components/logs/types'
import { formatRelative } from '@/lib/relativeTime'

interface RecentCrashesSectionProps {
  containerName: string
}

export function RecentCrashesSection({ containerName }: RecentCrashesSectionProps): JSX.Element {
  const crashesResult = useContainerCrashes(containerName)
  const [selectedCrashId, setSelectedCrashId] = useState<string | null>(null)

  return (
    <section
      aria-label="Recent crashes"
      className="rounded-md border border-border bg-card p-3"
      data-testid="recent-crashes-section"
    >
      <h2 className="mb-2 text-sm font-semibold">Recent crashes</h2>

      {crashesResult.isError && <ErrorDisplay error={crashesResult.error} />}
      {crashesResult.isPending && <div className="text-sm text-muted-foreground">Loading…</div>}

      {crashesResult.data && crashesResult.data.crashes.length === 0 && (
        <div className="text-sm text-muted-foreground" data-testid="recent-crashes-empty">
          No crashes recorded.
        </div>
      )}

      {crashesResult.data && crashesResult.data.crashes.length > 0 && (
        <ul className="space-y-2">
          {crashesResult.data.crashes.map((crash) => {
            const expanded = selectedCrashId === crash.crash_id
            return (
              <li
                key={crash.crash_id}
                className="rounded border border-border"
                data-testid="crash-row"
              >
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm"
                  onClick={() => setSelectedCrashId(expanded ? null : crash.crash_id)}
                  data-testid={`crash-expand-${crash.crash_id}`}
                  aria-expanded={expanded}
                >
                  <span className="flex items-center gap-2">
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs tabular-nums">
                      exit {crash.exit_code}
                    </span>
                    <span title={crash.finished_at}>{formatRelative(crash.finished_at)}</span>
                  </span>
                  <span className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="tabular-nums">{crash.line_count} lines</span>
                    {crash.truncated && <span title="Log window truncated">truncated</span>}
                    {crash.degraded && (
                      <span title="VictoriaLogs unavailable at capture time">degraded</span>
                    )}
                  </span>
                </button>
                {expanded && (
                  <CrashDetailPanel containerName={containerName} crashId={crash.crash_id} />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

interface CrashDetailPanelProps {
  containerName: string
  crashId: string
}

function CrashDetailPanel({ containerName, crashId }: CrashDetailPanelProps): JSX.Element {
  const detailResult = useContainerCrashDetail(containerName, crashId, true)

  if (detailResult.isError) {
    return (
      <div className="px-3 pb-3">
        <ErrorDisplay error={detailResult.error} />
      </div>
    )
  }
  if (detailResult.isPending || !detailResult.data) {
    return <div className="px-3 pb-3 text-sm text-muted-foreground">Loading logs…</div>
  }

  const detail = detailResult.data
  const lines: LogLine[] = detail.lines
  const truncated = detail.truncated
  const logsQl = `container_name:"${containerName}" AND source_type:docker`

  const useLogs = (): UseLogsResult => ({
    lines,
    isLoading: false,
    isError: false,
    error: undefined,
    logStatus: 'available',
    truncated,
  })

  return (
    <div className="space-y-2 border-t border-border px-3 py-2" data-testid="crash-logviewer">
      <OpenInExplorerButton
        logsQl={logsQl}
        rangeStart={new Date(detail.window_start)}
        rangeEnd={new Date(detail.window_end)}
        label="Open in Explorer"
      />
      {/* min-w-0 lets the wrapped LogViewer shrink to the card width on mobile
          instead of overflowing horizontally (the rows omit min-w-max when
          wrap is on — see LogLineList). */}
      <div className="min-w-0">
        <LogViewer useLogs={useLogs} wrap emptyStateCopy="No log lines captured for this crash." />
      </div>
    </div>
  )
}
