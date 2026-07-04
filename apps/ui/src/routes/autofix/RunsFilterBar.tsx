import type { JSX } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { useRunbooks } from '@/api/runbooks'
import type { RunsListFilters } from '@/api/autofix-runs'

interface Props {
  filters: RunsListFilters
}

const ANY = '__any__' // sentinel for "no filter"

export function RunsFilterBar({ filters }: Props): JSX.Element {
  const navigate = useNavigate()
  const runbooks = useRunbooks()

  const patch = (partial: Partial<RunsListFilters> & { page?: number }) => {
    void navigate({
      to: '/autofix/history',
      search: {
        runbook_id: 'runbook_id' in partial ? partial.runbook_id : filters.runbook_id,
        mode: 'mode' in partial ? partial.mode : filters.mode,
        outcome: 'outcome' in partial ? partial.outcome : filters.outcome,
        initiator: 'initiator' in partial ? partial.initiator : filters.initiator,
        since: 'since' in partial ? partial.since : filters.since,
        until: 'until' in partial ? partial.until : filters.until,
        page: 1,
      },
    })
  }

  const reset = () => {
    void navigate({
      to: '/autofix/history',
      search: {
        runbook_id: undefined,
        mode: undefined,
        outcome: undefined,
        initiator: undefined,
        since: undefined,
        until: undefined,
        page: 1,
      }, // clears everything; caller re-applies 30d defaults
    })
  }

  const sinceInput = (filters.since ?? '').slice(0, 10) // YYYY-MM-DD for <input type="date">
  const untilInput = (filters.until ?? '').slice(0, 10)

  return (
    <div className="flex flex-wrap items-end gap-3 rounded-md border p-3">
      <div className="min-w-[180px]">
        <Label htmlFor="runbook-filter">Runbook</Label>
        <Select
          id="runbook-filter"
          value={filters.runbook_id ?? ANY}
          onChange={(e) => {
            const v = e.currentTarget.value
            patch({ runbook_id: v === ANY ? undefined : v })
          }}
          data-testid="filter-runbook"
        >
          <option value={ANY}>Any</option>
          {(runbooks.data?.items ?? []).map((rb) => (
            <option key={rb.id} value={rb.id}>
              {basename(rb.path)}
            </option>
          ))}
        </Select>
      </div>

      <div className="min-w-[140px]">
        <Label htmlFor="mode-filter">Mode</Label>
        <Select
          id="mode-filter"
          value={filters.mode ?? ANY}
          onChange={(e) => {
            const v = e.currentTarget.value
            patch({ mode: v === ANY ? undefined : (v as 'dry_run' | 'real') })
          }}
          data-testid="filter-mode"
        >
          <option value={ANY}>Any</option>
          <option value="dry_run">Dry-run</option>
          <option value="real">Real</option>
        </Select>
      </div>

      <div className="min-w-[140px]">
        <Label htmlFor="outcome-filter">Outcome</Label>
        <Select
          id="outcome-filter"
          value={filters.outcome ?? ANY}
          onChange={(e) => {
            const v = e.currentTarget.value
            patch({
              outcome:
                v === ANY ? undefined : (v as 'success' | 'failure' | 'killed' | 'in_flight'),
            })
          }}
          data-testid="filter-outcome"
        >
          <option value={ANY}>Any</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="killed">Killed</option>
          <option value="in_flight">In flight</option>
        </Select>
      </div>

      <div className="min-w-[140px]">
        <Label htmlFor="initiator-filter">Initiator</Label>
        <Select
          id="initiator-filter"
          value={filters.initiator ?? ANY}
          onChange={(e) => {
            const v = e.currentTarget.value
            patch({ initiator: v === ANY ? undefined : (v as 'alert' | 'operator') })
          }}
          data-testid="filter-initiator"
        >
          <option value={ANY}>Any</option>
          <option value="alert">Alert</option>
          <option value="operator">Operator</option>
        </Select>
      </div>

      <div>
        <Label htmlFor="since-filter">Since</Label>
        <Input
          id="since-filter"
          type="date"
          value={sinceInput}
          onChange={(e) => patch({ since: dateInputToIso(e.target.value, 'start') })}
          data-testid="filter-since"
        />
      </div>

      <div>
        <Label htmlFor="until-filter">Until</Label>
        <Input
          id="until-filter"
          type="date"
          value={untilInput}
          onChange={(e) => patch({ until: dateInputToIso(e.target.value, 'end') })}
          data-testid="filter-until"
        />
      </div>

      <Button variant="ghost" onClick={reset} data-testid="filter-reset">
        Reset
      </Button>
    </div>
  )
}

function basename(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}

function dateInputToIso(yyyyMmDd: string, edge: 'start' | 'end'): string | undefined {
  if (!yyyyMmDd) return undefined
  const suffix = edge === 'start' ? 'T00:00:00.000Z' : 'T23:59:59.999Z'
  return `${yyyyMmDd}${suffix}`
}
