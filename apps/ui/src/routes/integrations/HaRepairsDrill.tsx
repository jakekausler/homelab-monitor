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
  const description =
    row.description != null && row.description.trim() !== '' ? row.description : null
  const learnMoreUrl = row.learn_more_url
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate">
          <span className="font-medium">{row.issue_id}</span>{' '}
          <span className="text-xs text-muted-foreground">{row.domain}</span>
        </span>
        <Badge variant={severityVariant(row.severity)} className="shrink-0">
          {row.severity}
        </Badge>
      </div>
      {description != null && (
        <p className="line-clamp-2 text-xs text-muted-foreground">{description}</p>
      )}
      {learnMoreUrl != null && (
        <a
          href={learnMoreUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block text-xs text-muted-foreground hover:text-foreground"
        >
          Learn more ↗
        </a>
      )}
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
