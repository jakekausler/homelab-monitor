import { formatAge } from '@/lib/relativeTime'

import { useHomeAssistantCadence } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaCadenceAutomationRow } from './types'

function renderRow(row: HaCadenceAutomationRow) {
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

export function HaCadenceAutomationsDrill() {
  const result = useHomeAssistantCadence()
  return (
    <DrillList<HaCadenceAutomationRow>
      items={result.data?.automations ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No idle automations"
      total={result.data?.automations_total ?? 0}
      returned={result.data?.automations_returned ?? 0}
      orderingLabel="most idle first"
      keyExtractor={(row) => row.entity_id}
    />
  )
}
