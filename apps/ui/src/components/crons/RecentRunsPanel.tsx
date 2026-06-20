import { Link } from '@tanstack/react-router'
import { ArrowRight } from 'lucide-react'

import { useListCronRuns } from '@/api/crons'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { RunStateBadge } from '@/components/crons/badges'
import { formatAbsolute, formatDuration, formatRelative } from '@/lib/relativeTime'

type CronRunOut = Schema<'CronRunOut'>

export interface RecentRunsPanelProps {
  fingerprint: string
}

export function RecentRunsPanel({ fingerprint }: RecentRunsPanelProps) {
  const runs = useListCronRuns(fingerprint, { limit: 5 })

  return (
    <Card aria-labelledby="panel-recent-runs" className="lg:col-span-2">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle id="panel-recent-runs">Recent runs</CardTitle>
        <Link
          to="/integrations/crons/$fingerprint/runs"
          params={{ fingerprint }}
          search={{ cursor: undefined, state: undefined }}
          className="inline-flex items-center text-sm text-primary hover:underline"
          data-testid="view-all-runs-link"
        >
          View all runs
          <ArrowRight className="ml-1 size-4" />
        </Link>
      </CardHeader>
      <CardContent className="text-sm">
        {runs.isLoading && <p className="text-muted-foreground">Loading runs…</p>}
        {runs.error && (
          <p role="alert" className="text-red-600">
            {runs.error.message}
          </p>
        )}
        {runs.data && runs.data.items.length === 0 && (
          <p className="text-muted-foreground" data-testid="recent-runs-empty">
            No runs recorded yet.
          </p>
        )}
        {runs.data && runs.data.items.length > 0 && (
          <ul className="divide-y divide-border" data-testid="recent-runs-list">
            {runs.data.items.map((r) => (
              <RecentRunRow key={r.run_id} fingerprint={fingerprint} run={r} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function RecentRunRow({ fingerprint, run }: { fingerprint: string; run: CronRunOut }) {
  const flags = run.anomaly_flags.length > 0 ? run.anomaly_flags.split(',') : []
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2">
      <Link
        to="/integrations/crons/$fingerprint/runs/$run_id"
        params={{ fingerprint, run_id: run.run_id }}
        className="text-primary hover:underline"
        title={formatAbsolute(run.started_at)}
      >
        {formatRelative(run.started_at)}
      </Link>
      <div className="flex flex-wrap items-center gap-2">
        <RunStateBadge state={run.state} />
        <span className="text-xs text-muted-foreground">
          {formatDuration(run.duration_seconds)}
        </span>
        {run.exit_code !== null && run.exit_code !== 0 && (
          <span className="text-xs text-muted-foreground">exit {run.exit_code}</span>
        )}
        {flags.length > 0 && (
          <Badge variant="warn" data-testid="anomaly-badge">
            {flags.length === 1 ? flags[0] : `${String(flags.length)} anomalies`}
          </Badge>
        )}
      </div>
    </li>
  )
}
