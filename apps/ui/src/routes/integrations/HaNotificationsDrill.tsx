import { formatAge } from '@/lib/relativeTime'

import { useHomeAssistantNotifications } from '@/api/home_assistant'

import { DrillList } from './DrillList'
import type { HaNotificationRow } from './types'

function ageSecondsFrom(createdAt: string | null): number | null {
  if (createdAt === null || createdAt === '') return null
  const parsed = Date.parse(createdAt)
  if (Number.isNaN(parsed)) return null
  return (Date.now() - parsed) / 1000
}

function renderRow(row: HaNotificationRow) {
  const ageSeconds = ageSecondsFrom(row.created_at)
  const title = row.title === null || row.title === '' ? '(untitled)' : row.title
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="truncate text-sm font-medium">{title}</h4>
        {ageSeconds !== null && row.created_at !== null && (
          <time
            dateTime={row.created_at}
            className="shrink-0 tabular-nums text-xs text-muted-foreground"
          >
            {formatAge(ageSeconds)}
          </time>
        )}
      </div>
      <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{row.message}</p>
    </div>
  )
}

export function HaNotificationsDrill() {
  const result = useHomeAssistantNotifications()
  return (
    <DrillList<HaNotificationRow>
      items={result.data?.rows ?? []}
      isPending={result.isPending}
      error={result.error}
      renderRow={renderRow}
      emptyLabel="No notifications"
      total={result.data?.total ?? 0}
      returned={result.data?.returned ?? 0}
      keyExtractor={(row) => row.notification_id}
    />
  )
}
