import { formatAge } from '@/lib/relativeTime'

import { useHomeAssistantCadence } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaCadenceScriptRow } from './types'

function renderRow(row: HaCadenceScriptRow) {
  const primary = row.friendly_name?.trim() ? row.friendly_name : row.entity_id
  const hasName = Boolean(row.friendly_name?.trim())
  const age =
    row.last_triggered_age_seconds === null ? 'never' : formatAge(row.last_triggered_age_seconds)
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{primary}</span>
        <span className="shrink-0 tabular-nums text-xs text-muted-foreground">{age}</span>
      </div>
      {hasName && <p className="truncate text-xs text-muted-foreground">{row.entity_id}</p>}
    </div>
  )
}

export function HaCadenceScriptsDrill() {
  const result = useHomeAssistantCadence()
  return (
    <DrillList<HaCadenceScriptRow>
      items={result.data?.scripts ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No idle scripts"
      total={result.data?.scripts_total ?? 0}
      returned={result.data?.scripts_returned ?? 0}
      orderingLabel="most idle first"
      keyExtractor={(row) => row.entity_id}
    />
  )
}
