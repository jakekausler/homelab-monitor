import type { JSX } from 'react'

import { EmptyState } from '@/components/EmptyState'

import type { HaConfigEntriesSummary } from './types'

interface HaIntegrationStatusWidgetProps {
  configEntries: HaConfigEntriesSummary
  repairs: number
  notifications: number
}

export function HaIntegrationStatusWidget({
  configEntries,
  repairs,
  notifications,
}: HaIntegrationStatusWidgetProps): JSX.Element {
  const { loaded, error } = configEntries

  if (error === 0 && repairs === 0 && notifications === 0) {
    return <EmptyState>All integrations healthy</EmptyState>
  }

  return (
    <dl className="grid grid-cols-2 gap-2 text-sm">
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Loaded</dt>
        <dd className="tabular-nums">{loaded}</dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Errors</dt>
        <dd className={error > 0 ? 'tabular-nums text-red-700 dark:text-red-300' : 'tabular-nums'}>
          {error}
        </dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Repairs</dt>
        <dd
          className={
            repairs > 0 ? 'tabular-nums text-amber-700 dark:text-amber-300' : 'tabular-nums'
          }
        >
          {repairs}
        </dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Notifications</dt>
        <dd
          className={
            notifications > 0 ? 'tabular-nums text-amber-700 dark:text-amber-300' : 'tabular-nums'
          }
        >
          {notifications}
        </dd>
      </div>
    </dl>
  )
}
