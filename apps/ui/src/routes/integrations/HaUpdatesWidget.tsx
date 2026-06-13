import type { JSX } from 'react'

import { EmptyState } from '@/components/EmptyState'

import type { HaUpdatesSummary } from './types'

interface HaUpdatesWidgetProps {
  updates: HaUpdatesSummary
}

export function HaUpdatesWidget({ updates }: HaUpdatesWidgetProps): JSX.Element {
  const { available } = updates

  if (available === 0) {
    return <EmptyState>All up to date</EmptyState>
  }

  return (
    <dl className="grid grid-cols-1 gap-2 text-sm">
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Available</dt>
        <dd
          className={
            available > 0 ? 'tabular-nums text-amber-700 dark:text-amber-300' : 'tabular-nums'
          }
        >
          {available}
        </dd>
      </div>
    </dl>
  )
}
