import { useHomeAssistantBatteries } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaBatteryRow } from './types'

function levelClass(level: number): string {
  if (level < 10) return 'tabular-nums text-red-700 dark:text-red-300'
  if (level < 20) return 'tabular-nums text-amber-700 dark:text-amber-300'
  return 'tabular-nums'
}

function renderRow(row: HaBatteryRow) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="truncate">
        <span className="font-medium">{row.entity_id}</span>{' '}
        <span className="text-xs text-muted-foreground">{row.domain}</span>
      </span>
      <span className={`shrink-0 text-xs ${levelClass(row.level)}`}>{row.level}%</span>
    </div>
  )
}

export function HaBatteriesDrill() {
  const result = useHomeAssistantBatteries()
  return (
    <DrillList<HaBatteryRow>
      items={result.data?.batteries ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="All batteries healthy"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row) => row.entity_id}
    />
  )
}
