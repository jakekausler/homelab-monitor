import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'

import { useGetCron, useListCronRuns } from '@/api/crons'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { RunStateBadge, type RunState } from '@/components/crons/badges'
import { formatAbsolute, formatDuration, formatRelative } from '@/lib/relativeTime'

type CronRunOut = Schema<'CronRunOut'>

const STATE_OPTIONS: ReadonlyArray<{ value: '' | RunState; label: string }> = [
  { value: '', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'ok', label: 'Ok' },
  { value: 'fail', label: 'Fail' },
  { value: 'unknown', label: 'Unknown' },
]

export function CronRunsListPage() {
  const params = useParams({ strict: false })
  const fingerprint = params.fingerprint ?? ''
  const search = useSearch({ from: '/protected/integrations/crons/$fingerprint/runs' })
  const navigate = useNavigate()
  const cron = useGetCron(fingerprint, { includeHidden: true })
  const runs = useListCronRuns(fingerprint, {
    limit: 50,
    ...(search.cursor !== undefined && { cursor: search.cursor }),
    ...(search.state !== undefined && { state: search.state }),
  })

  const handleStateChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const v = e.target.value
    const next: RunState | undefined =
      v === 'running' || v === 'ok' || v === 'fail' || v === 'unknown' ? v : undefined
    void navigate({
      to: '/integrations/crons/$fingerprint/runs',
      params: { fingerprint },
      search: { cursor: undefined, state: next },
    })
  }

  const handleNextPage = () => {
    if (runs.data?.next_cursor != null) {
      void navigate({
        to: '/integrations/crons/$fingerprint/runs',
        params: { fingerprint },
        search: {
          cursor: runs.data.next_cursor,
          ...(search.state !== undefined && { state: search.state }),
        },
      })
    }
  }

  const items = runs.data?.items ?? []

  return (
    <div className="space-y-4">
      <Link
        to="/integrations/crons/$fingerprint"
        params={{ fingerprint }}
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="mr-1 size-4" />
        Back to {cron.data?.cron.name ?? 'cron'}
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Run history</h1>
          {cron.data && (
            <p className="text-sm text-muted-foreground">
              {cron.data.cron.name} · {cron.data.cron.host}
            </p>
          )}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Filter by state</span>
          <Select
            value={search.state ?? ''}
            onChange={handleStateChange}
            className="w-40"
            aria-label="Filter by state"
          >
            {STATE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
        </label>
      </div>

      {runs.error && (
        <p role="alert" className="text-red-600">
          {runs.error.message}
        </p>
      )}

      {runs.isLoading && <p className="text-muted-foreground">Loading runs…</p>}

      {!runs.isLoading && items.length === 0 && (
        <div
          className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground"
          data-testid="runs-empty"
        >
          No runs match these filters.
        </div>
      )}

      {items.length > 0 && (
        <>
          {/* Desktop: table */}
          <RunsDesktopTable fingerprint={fingerprint} items={items} />
          {/* Mobile: card list */}
          <RunsMobileCards fingerprint={fingerprint} items={items} />
        </>
      )}

      {runs.data?.next_cursor != null && (
        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={handleNextPage} data-testid="next-page">
            Next page
          </Button>
        </div>
      )}
    </div>
  )
}

function RunsDesktopTable({ fingerprint, items }: { fingerprint: string; items: CronRunOut[] }) {
  return (
    <div
      className="hidden overflow-x-auto rounded-md border border-border bg-card md:block"
      data-testid="runs-desktop"
    >
      <table className="min-w-full divide-y divide-border text-sm">
        <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left">
              Started
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Duration
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              State
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Source
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Exit
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Anomalies
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Overlap
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((r) => {
            const flags = r.anomaly_flags.length > 0 ? r.anomaly_flags.split(',') : []
            return (
              <tr key={r.run_id} className="hover:bg-accent/30">
                <td className="px-3 py-2">
                  <Link
                    to="/integrations/crons/$fingerprint/runs/$run_id"
                    params={{ fingerprint, run_id: r.run_id }}
                    className="text-primary hover:underline"
                    title={formatAbsolute(r.started_at)}
                  >
                    {formatRelative(r.started_at)}
                  </Link>
                </td>
                <td className="px-3 py-2 text-xs">{formatDuration(r.duration_seconds)}</td>
                <td className="px-3 py-2">
                  <RunStateBadge state={r.state} />
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">{r.source}</td>
                <td className="px-3 py-2 text-xs">{r.exit_code ?? '—'}</td>
                <td className="px-3 py-2">
                  {flags.length === 0 ? (
                    <span className="text-xs text-muted-foreground">—</span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {flags.map((f) => (
                        <Badge key={f} variant="warn" data-testid="anomaly-badge">
                          {f}
                        </Badge>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  {r.overlapping ? (
                    <Badge variant="warn" data-testid="overlap-badge">
                      Overlap
                    </Badge>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function RunsMobileCards({ fingerprint, items }: { fingerprint: string; items: CronRunOut[] }) {
  return (
    <ul className="space-y-2 md:hidden" data-testid="runs-mobile">
      {items.map((r) => {
        const flags = r.anomaly_flags.length > 0 ? r.anomaly_flags.split(',') : []
        return (
          <li key={r.run_id} className="rounded-md border border-border bg-card p-3 text-sm">
            <Link
              to="/integrations/crons/$fingerprint/runs/$run_id"
              params={{ fingerprint, run_id: r.run_id }}
              className="block space-y-2"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-primary" title={formatAbsolute(r.started_at)}>
                  {formatRelative(r.started_at)}
                </span>
                <div className="flex items-center gap-2">
                  <RunStateBadge state={r.state} />
                  <span className="text-xs text-muted-foreground">
                    {formatDuration(r.duration_seconds)}
                  </span>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>
                  {r.source} · exit {r.exit_code ?? '—'}
                </span>
                {flags.map((f) => (
                  <Badge key={f} variant="warn" data-testid="anomaly-badge">
                    {f}
                  </Badge>
                ))}
                {r.overlapping && (
                  <Badge variant="warn" data-testid="overlap-badge">
                    Overlap
                  </Badge>
                )}
              </div>
            </Link>
          </li>
        )
      })}
    </ul>
  )
}
