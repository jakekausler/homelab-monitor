import { useEffect, useState } from 'react'
import { Plus, Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { titleCase } from './badges'

export interface ToolbarFilters {
  host?: string
  integration_mode?: 'observe' | 'heartbeat' | 'both'
  state?: 'unknown' | 'running' | 'ok' | 'failed' | 'late'
  enabled?: boolean
  q?: string
  include_archived: boolean
}

export interface CronsToolbarProps {
  filters: ToolbarFilters
  knownHosts: string[]
  onFiltersChange: (next: ToolbarFilters) => void
  onAddClick: () => void
}

const SEARCH_DEBOUNCE_MS = 250

export function CronsToolbar({
  filters,
  knownHosts,
  onFiltersChange,
  onAddClick,
}: CronsToolbarProps) {
  const [searchInput, setSearchInput] = useState(filters.q ?? '')

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (searchInput === (filters.q ?? '')) return
      const next = { ...filters }
      if (searchInput.length > 0) {
        next.q = searchInput
      } else {
        delete next.q
      }
      onFiltersChange(next)
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput])

  const update = (next: ToolbarFilters) => {
    onFiltersChange(next)
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card p-3">
      <div className="relative grow min-w-[180px]">
        <Search className="absolute left-2 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Search by name or command"
          placeholder="Search name or command…"
          className="pl-8"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
        />
      </div>

      <Select
        aria-label="Filter by host"
        className="w-auto"
        value={filters.host ?? ''}
        onChange={(e) => {
          const next = { ...filters }
          if (e.target.value === '') {
            delete next.host
          } else {
            next.host = e.target.value
          }
          update(next)
        }}
      >
        <option value="">All hosts</option>
        {knownHosts.map((h) => (
          <option key={h} value={h}>
            {h}
          </option>
        ))}
      </Select>

      <Select
        aria-label="Filter by integration mode"
        className="w-auto"
        value={filters.integration_mode ?? ''}
        onChange={(e) => {
          const next = { ...filters }
          if ((e.target.value as ToolbarFilters['integration_mode'] | '') === '') {
            delete next.integration_mode
          } else {
            next.integration_mode = e.target.value as 'observe' | 'heartbeat' | 'both'
          }
          update(next)
        }}
      >
        <option value="">All modes</option>
        <option value="observe">{titleCase('observe')}</option>
        <option value="heartbeat">{titleCase('heartbeat')}</option>
        <option value="both">{titleCase('both')}</option>
      </Select>

      <Select
        aria-label="Filter by state"
        className="w-auto"
        value={filters.state ?? ''}
        onChange={(e) => {
          const next = { ...filters }
          if ((e.target.value as ToolbarFilters['state'] | '') === '') {
            delete next.state
          } else {
            next.state = e.target.value as 'unknown' | 'running' | 'ok' | 'failed' | 'late'
          }
          update(next)
        }}
      >
        <option value="">All states</option>
        <option value="unknown">{titleCase('unknown')}</option>
        <option value="running">{titleCase('running')}</option>
        <option value="ok">{titleCase('ok')}</option>
        <option value="failed">{titleCase('failed')}</option>
        <option value="late">{titleCase('late')}</option>
      </Select>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={filters.include_archived}
          onChange={(e) => update({ ...filters, include_archived: e.target.checked })}
        />
        Show archived
      </label>

      <div className="ml-auto flex gap-2">
        <Button onClick={onAddClick}>
          <Plus className="mr-1 size-4" />
          Add cron
        </Button>
      </div>
    </div>
  )
}
