import { useEffect, useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { titleCase } from './badges'

export interface ToolbarFilters {
  host?: string
  state?: 'unknown' | 'running' | 'ok' | 'failed' | 'late'
  wrapper_installed?: boolean
  q?: string
  include_hidden: boolean
  include_soft_deleted: boolean
}

export interface CronsToolbarProps {
  filters: ToolbarFilters
  knownHosts: string[]
  onFiltersChange: (next: ToolbarFilters) => void
  onDiscoverNow?: () => void
  isDiscovering?: boolean
}

const SEARCH_DEBOUNCE_MS = 250

export function CronsToolbar({
  filters,
  knownHosts,
  onFiltersChange,
  onDiscoverNow,
  isDiscovering = false,
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
      {onDiscoverNow !== undefined && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Discover now"
              onClick={onDiscoverNow}
              disabled={isDiscovering}
            >
              <RefreshCw className={isDiscovering ? 'size-4 animate-spin' : 'size-4'} />
              <span className="sr-only">Discover now</span>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Discover now</TooltipContent>
        </Tooltip>
      )}

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

      <Select
        aria-label="Filter by wrapper status"
        className="w-auto"
        value={
          filters.wrapper_installed === undefined ? '' : filters.wrapper_installed ? 'yes' : 'no'
        }
        onChange={(e) => {
          const next = { ...filters }
          if (e.target.value === '') {
            delete next.wrapper_installed
          } else if (e.target.value === 'yes') {
            next.wrapper_installed = true
          } else {
            next.wrapper_installed = false
          }
          update(next)
        }}
      >
        <option value="">Any wrapper</option>
        <option value="yes">Wrapper installed</option>
        <option value="no">No wrapper</option>
      </Select>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={filters.include_hidden}
          onChange={(e) => update({ ...filters, include_hidden: e.target.checked })}
        />
        Show hidden
      </label>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={filters.include_soft_deleted}
          onChange={(e) => update({ ...filters, include_soft_deleted: e.target.checked })}
        />
        Show soft-deleted
      </label>
    </div>
  )
}
