import { useState } from 'react'
import type { JSX } from 'react'

import {
  useContainerHealthcheckIncidents,
  useContainerHealthcheckIncidentDetail,
} from '@/api/docker'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { LogViewer } from '@/components/logs/LogViewer'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import type { LogLine, UseLogsResult } from '@/components/logs/types'
import { formatRelative } from '@/lib/relativeTime'

interface RecentHealthcheckIncidentsSectionProps {
  containerName: string
}

function transitionLabel(previous: string | null): string {
  return previous ? `${previous} → unhealthy` : '→ unhealthy'
}

export function RecentHealthcheckIncidentsSection({
  containerName,
}: RecentHealthcheckIncidentsSectionProps): JSX.Element {
  const incidentsResult = useContainerHealthcheckIncidents(containerName)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  return (
    <section
      aria-label="Recent healthcheck incidents"
      className="rounded-md border border-border bg-card p-3"
      data-testid="recent-healthcheck-section"
    >
      <h2 className="mb-2 text-sm font-semibold">Recent healthcheck incidents</h2>

      {incidentsResult.isError && <ErrorDisplay error={incidentsResult.error} />}
      {incidentsResult.isPending && <div className="text-sm text-muted-foreground">Loading…</div>}

      {incidentsResult.data && incidentsResult.data.incidents.length === 0 && (
        <div className="text-sm text-muted-foreground" data-testid="recent-healthcheck-empty">
          No healthcheck incidents recorded.
        </div>
      )}

      {incidentsResult.data && incidentsResult.data.incidents.length > 0 && (
        <ul className="space-y-2">
          {incidentsResult.data.incidents.map((incident) => {
            const expanded = selectedId === incident.incident_id
            return (
              <li
                key={incident.incident_id}
                className="rounded border border-border"
                data-testid="healthcheck-row"
              >
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm"
                  onClick={() => setSelectedId(expanded ? null : incident.incident_id)}
                  data-testid={`healthcheck-expand-${incident.incident_id}`}
                  aria-expanded={expanded}
                >
                  <span className="flex items-center gap-2">
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                      {transitionLabel(incident.previous_healthcheck)}
                    </span>
                    <span title={incident.healthcheck_changed_at}>
                      {formatRelative(incident.healthcheck_changed_at)}
                    </span>
                  </span>
                  <span className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="tabular-nums">{incident.line_count} lines</span>
                    {incident.truncated && <span title="Log window truncated">truncated</span>}
                    {incident.degraded && (
                      <span title="VictoriaLogs unavailable at capture time">degraded</span>
                    )}
                  </span>
                </button>
                {expanded && (
                  <HealthcheckDetailPanel
                    containerName={containerName}
                    incidentId={incident.incident_id}
                  />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

interface HealthcheckDetailPanelProps {
  containerName: string
  incidentId: string
}

function HealthcheckDetailPanel({
  containerName,
  incidentId,
}: HealthcheckDetailPanelProps): JSX.Element {
  const detailResult = useContainerHealthcheckIncidentDetail(containerName, incidentId, true)

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
    <div className="space-y-2 border-t border-border px-3 py-2" data-testid="healthcheck-logviewer">
      <OpenInExplorerButton
        logsQl={logsQl}
        rangeStart={new Date(detail.window_start)}
        rangeEnd={new Date(detail.window_end)}
        label="Open in Explorer"
      />
      {/* min-w-0 lets the wrapped LogViewer shrink to the card width on mobile
          instead of overflowing horizontally (the 032 mobile-overflow fix). */}
      <div className="min-w-0">
        <LogViewer
          useLogs={useLogs}
          wrap
          emptyStateCopy="No log lines captured for this incident."
        />
      </div>
    </div>
  )
}
