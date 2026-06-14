import { useHomeAssistantUpdates } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaUpdateRow } from './types'

function renderRow(row: HaUpdateRow) {
  const label = row.title.trim() === '' ? row.entity_id : row.title
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="truncate font-medium">{label}</span>
      <span className="shrink-0 text-xs text-muted-foreground">{row.entity_id}</span>
    </div>
  )
}

export function HaUpdatesDrill() {
  const result = useHomeAssistantUpdates()
  return (
    <DrillList<HaUpdateRow>
      items={result.data?.updates ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No updates pending"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row) => row.entity_id}
    />
  )
}
