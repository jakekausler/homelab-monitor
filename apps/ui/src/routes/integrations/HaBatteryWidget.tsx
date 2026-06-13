import type { JSX } from 'react'

import { EmptyState } from '@/components/EmptyState'

import type { HaBatterySummary } from './types'

interface HaBatteryWidgetProps {
  battery: HaBatterySummary
}

export function HaBatteryWidget({ battery }: HaBatteryWidgetProps): JSX.Element {
  const { low, critical } = battery

  if (low === 0 && critical === 0) {
    return <EmptyState>All batteries healthy</EmptyState>
  }

  return (
    <dl className="grid grid-cols-2 gap-2 text-sm">
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Low</dt>
        <dd
          className={low > 0 ? 'tabular-nums text-amber-700 dark:text-amber-300' : 'tabular-nums'}
        >
          {low}
        </dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Critical</dt>
        <dd
          className={critical > 0 ? 'tabular-nums text-red-700 dark:text-red-300' : 'tabular-nums'}
        >
          {critical}
        </dd>
      </div>
    </dl>
  )
}
