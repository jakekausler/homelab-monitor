import { useMemo, type JSX } from 'react'
import { EmptyState } from '@/components/EmptyState'
import { useSearch, useNavigate } from '@tanstack/react-router'
import { useRunsList, RUNS_PAGE_SIZE } from '@/api/autofix-runs'
import { RunsFilterBar } from './RunsFilterBar'
import { RunsTable } from './RunsTable'

export function RunsHistoryPage(): JSX.Element {
  const search = useSearch({ from: '/protected/autofix/history' })
  const navigate = useNavigate()
  const page = search.page ?? 1
  // IMPORTANT: memoize the filters object because defaultSinceIso()/defaultUntilIso()
  // return fresh millisecond-different ISO strings each call. Without useMemo, the
  // query key churns every render, useRunsList refetches in a loop, and the page hangs
  // forever on "Loading runs…" (STAGE-009-011 regression item).
  const filters = useMemo(
    () => ({
      runbook_id: search.runbook_id,
      mode: search.mode,
      outcome: search.outcome,
      initiator: search.initiator,
      since: search.since ?? defaultSinceIso(),
      until: search.until ?? defaultUntilIso(),
    }),
    [search.runbook_id, search.mode, search.outcome, search.initiator, search.since, search.until],
  )
  const query = useRunsList(filters, page)

  return (
    <div className="space-y-4 p-4">
      <div>
        <h1 className="text-2xl font-semibold">Auto-fix history</h1>
        <p className="text-sm text-muted-foreground">
          Runs from the auto-fix subsystem — dry-run and real, most recent first.
        </p>
      </div>

      <RunsFilterBar filters={filters} />

      {query.isLoading && <p className="text-sm text-muted-foreground">Loading runs…</p>}
      {query.isError && (
        <p role="alert" className="text-sm text-destructive">
          Failed to load runs: {query.error.message}
        </p>
      )}
      {query.data !== undefined && query.data.items.length === 0 && (
        <EmptyState>No runs match these filters.</EmptyState>
      )}
      {query.data !== undefined && query.data.items.length > 0 && (
        <>
          <RunsTable runs={query.data.items} />
          <Pagination
            page={page}
            totalCount={query.data.total_count}
            onChange={(nextPage) => {
              void navigate({
                to: '/autofix/history',
                search: {
                  runbook_id: search.runbook_id,
                  mode: search.mode,
                  outcome: search.outcome,
                  initiator: search.initiator,
                  since: search.since,
                  until: search.until,
                  page: nextPage,
                },
              })
            }}
          />
        </>
      )}
    </div>
  )
}

function defaultSinceIso(): string {
  const now = Date.now()
  return new Date(now - 30 * 24 * 60 * 60 * 1000).toISOString()
}
function defaultUntilIso(): string {
  return new Date().toISOString()
}

interface PaginationProps {
  page: number
  totalCount: number
  onChange: (nextPage: number) => void
}

function Pagination({ page, totalCount, onChange }: PaginationProps): JSX.Element {
  const totalPages = Math.max(1, Math.ceil(totalCount / RUNS_PAGE_SIZE))
  return (
    <div className="flex items-center justify-between text-sm">
      <span data-testid="pagination-summary">
        Page {page} of {totalPages} · {totalCount} run{totalCount === 1 ? '' : 's'}
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          className="rounded border px-2 py-1 disabled:opacity-50"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          data-testid="pagination-prev"
        >
          Prev
        </button>
        <button
          type="button"
          className="rounded border px-2 py-1 disabled:opacity-50"
          disabled={page >= totalPages}
          onClick={() => onChange(page + 1)}
          data-testid="pagination-next"
        >
          Next
        </button>
      </div>
    </div>
  )
}
