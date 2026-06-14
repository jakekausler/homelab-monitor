import { Badge } from '@/components/ui/badge'

import { useHomeAssistantRepairs } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaRepairRow } from './types'

type BadgeVariant = 'critical' | 'warn' | 'muted'

function severityVariant(severity: string): BadgeVariant {
  const s = severity.toLowerCase()
  if (s === 'critical' || s === 'error') return 'critical'
  if (s === 'warning' || s === 'warn') return 'warn'
  return 'muted'
}

function renderRow(row: HaRepairRow) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="truncate">
        <span className="font-medium">{row.issue_id}</span>{' '}
        <span className="text-xs text-muted-foreground">{row.domain}</span>
      </span>
      <Badge variant={severityVariant(row.severity)} className="shrink-0">
        {row.severity}
      </Badge>
    </div>
  )
}

export function HaRepairsDrill() {
  const result = useHomeAssistantRepairs()
  return (
    <DrillList<HaRepairRow>
      items={result.data?.repairs ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No active repairs"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row) => row.issue_id}
    />
  )
}
