import { formatAge } from '@/lib/relativeTime'

import { useHomeAssistantEntities } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaEntityRow } from './types'

function renderRow(row: HaEntityRow) {
  const primary = row.friendly_name?.trim() ? row.friendly_name : row.entity_id
  const hasName = Boolean(row.friendly_name?.trim())
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{primary}</span>
        <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
          {formatAge(row.last_changed_age_seconds)}
        </span>
      </div>
      {hasName && (
        <p className="truncate text-xs text-muted-foreground">
          {row.entity_id} · {row.domain}
        </p>
      )}
    </div>
  )
}

export function HaEntitiesDrill() {
  const result = useHomeAssistantEntities()
  return (
    <DrillList<HaEntityRow>
      items={result.data?.entities ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No unavailable entities"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      orderingLabel="stalest first"
      keyExtractor={(row) => row.entity_id}
    />
  )
}
