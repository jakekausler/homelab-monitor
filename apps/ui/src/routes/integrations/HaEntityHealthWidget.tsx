import type { JSX } from 'react'

import type { HaEntitiesSummary } from './types'

interface HaEntityHealthWidgetProps {
  entities: HaEntitiesSummary
}

export function HaEntityHealthWidget({ entities }: HaEntityHealthWidgetProps): JSX.Element {
  const { total, available, unavailable } = entities
  return (
    <dl className="grid grid-cols-2 gap-2 text-sm">
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Total</dt>
        <dd className="tabular-nums">{total}</dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Available</dt>
        <dd className="tabular-nums">{available}</dd>
      </div>
      <div>
        <dt className="text-xs uppercase tracking-wide text-muted-foreground">Unavailable</dt>
        <dd
          className={
            unavailable > 0 ? 'tabular-nums text-amber-700 dark:text-amber-300' : 'tabular-nums'
          }
        >
          {unavailable}
        </dd>
      </div>
    </dl>
  )
}
