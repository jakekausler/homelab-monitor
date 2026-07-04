import type { JSX } from 'react'
import type { KeyboardEvent } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Badge } from '@/components/ui/badge'
import { formatRelative, formatDuration } from '@/lib/relativeTime'
import { useNowTick } from '@/lib/useNowTick'
import type { Run } from '@/api/autofix-runs'

interface Props {
  runs: Run[]
}

export function RunsTable({ runs }: Props): JSX.Element {
  const navigate = useNavigate()
  const nowMs = useNowTick(1000)

  const goTo = (id: string) => {
    void navigate({
      to: '/autofix/history/$run_id',
      params: { run_id: id },
    })
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      goTo(id)
    }
  }

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-3 py-2">Runbook</th>
            <th className="px-3 py-2">Mode</th>
            <th className="px-3 py-2">Outcome</th>
            <th className="px-3 py-2">Started</th>
            <th className="px-3 py-2">Duration</th>
            <th className="px-3 py-2">Initiator</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.id}
              role="button"
              tabIndex={0}
              onClick={() => goTo(run.id)}
              onKeyDown={(e) => handleKeyDown(e, run.id)}
              className="cursor-pointer border-t hover:bg-muted/50 focus:bg-muted/50 focus:outline-none"
              data-testid={`runs-row-${run.id}`}
            >
              <td className="px-3 py-2 font-mono text-xs">{basename(run.runbook_path)}</td>
              <td className="px-3 py-2">
                <ModeBadge mode={run.mode} />
              </td>
              <td className="px-3 py-2">
                <OutcomeBadge outcome={run.outcome} />
              </td>
              <td className="px-3 py-2">{formatRelative(run.started_at, nowMs)}</td>
              <td className="px-3 py-2">
                {run.duration_ms == null ? '—' : formatDuration(run.duration_ms / 1000)}
              </td>
              <td className="px-3 py-2">
                {run.initiated_by === 'operator' ? (
                  <span className="text-muted-foreground">operator</span>
                ) : run.alert_id ? (
                  <span className="font-mono text-xs" data-testid={`runs-row-alert-${run.id}`}>
                    {run.alert_id.slice(0, 8)}
                  </span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ModeBadge({ mode }: { mode: Run['mode'] }): JSX.Element {
  if (mode === 'dry_run') {
    return (
      <Badge variant="secondary" data-testid="mode-badge-dry_run">
        Dry-run
      </Badge>
    )
  }
  return <Badge data-testid="mode-badge-real">Real</Badge>
}

export function OutcomeBadge({ outcome }: { outcome: Run['outcome'] }): JSX.Element {
  switch (outcome) {
    case 'success':
      return (
        <Badge className="bg-green-600 text-white" data-testid="outcome-badge-success">
          Success
        </Badge>
      )
    case 'failure':
      return (
        <Badge variant="critical" data-testid="outcome-badge-failure">
          Failure
        </Badge>
      )
    case 'killed':
      return (
        <Badge
          variant="outline"
          className="border-destructive text-destructive"
          data-testid="outcome-badge-killed"
        >
          Killed
        </Badge>
      )
    case 'in_flight':
      return (
        <Badge variant="secondary" className="animate-pulse" data-testid="outcome-badge-in_flight">
          In flight
        </Badge>
      )
    default:
      return <></>
  }
}

function basename(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}
