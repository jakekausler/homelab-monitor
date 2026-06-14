import { Badge } from '@/components/ui/badge'

import { useHomeAssistantConfigEntries } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaConfigEntryRow } from './types'

type BadgeVariant = 'critical' | 'warn' | 'muted'

function stateVariant(state: string): BadgeVariant {
  const s = state.toLowerCase()
  if (s === 'setup_error' || s === 'setup_retry' || s === 'failed_unload' || s === 'error') {
    return 'critical'
  }
  if (s === 'not_loaded' || s === 'migration_error') {
    return 'warn'
  }
  return 'muted'
}

function renderRow(row: HaConfigEntryRow) {
  const label = row.title.trim() === '' ? row.domain : row.title
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="truncate">
        <span className="font-medium">{label}</span>{' '}
        <span className="text-xs text-muted-foreground">{row.domain}</span>
      </span>
      <Badge variant={stateVariant(row.state)} className="shrink-0">
        {row.state}
      </Badge>
    </div>
  )
}

export function HaConfigEntriesDrill() {
  const result = useHomeAssistantConfigEntries()
  return (
    <DrillList<HaConfigEntryRow>
      items={result.data?.config_entries ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No integration errors"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row, index) => `${row.domain}-${index}`}
    />
  )
}
