import type { ReactNode } from 'react'

import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'

import type { ApiError } from '@/api/client'

/** Row count above which the list becomes a fixed-height scroll container. */
const SCROLL_ROW_THRESHOLD = 8

interface DrillListProps<T> {
  items: T[]
  isPending: boolean
  error: ApiError | null
  renderRow: (item: T, index: number) => ReactNode
  emptyLabel: string
  total: number
  returned: number
  /** Optional ordering suffix for the cap caption (e.g. "stalest first"). */
  orderingLabel?: string
  keyExtractor?: (item: T, index: number) => string
}

export function DrillList<T>({
  items,
  isPending,
  error,
  renderRow,
  emptyLabel,
  total,
  returned,
  orderingLabel,
  keyExtractor,
}: DrillListProps<T>) {
  if (isPending) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  if (error?.status === 502) {
    return (
      <div
        className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
        role="status"
        aria-live="polite"
      >
        Home Assistant metrics temporarily unavailable
      </div>
    )
  }

  if (error) {
    return <ErrorDisplay error={error} />
  }

  if (items.length === 0) {
    return <EmptyState>{emptyLabel}</EmptyState>
  }

  const scrollable = items.length > SCROLL_ROW_THRESHOLD
  const showCaption = total > returned
  const caption = orderingLabel
    ? `Showing ${returned} of ${total} — ${orderingLabel}`
    : `Showing ${returned} of ${total}`

  return (
    <div className="space-y-2">
      <ul
        className={
          scrollable ? 'max-h-80 divide-y divide-border overflow-y-auto' : 'divide-y divide-border'
        }
      >
        {items.map((item, index) => (
          <li key={keyExtractor ? keyExtractor(item, index) : index} className="py-2 text-sm">
            {renderRow(item, index)}
          </li>
        ))}
      </ul>
      {showCaption && <p className="text-xs text-muted-foreground">{caption}</p>}
    </div>
  )
}
