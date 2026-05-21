import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

interface EmptyStateProps {
  children: ReactNode
  className?: string
  /** Optional data-testid for test scoping. */
  testId?: string
}

/**
 * Standard empty-state card used across the app. Wraps children in a centered,
 * muted-foreground card with the project's standard rounded-border styling.
 */
export function EmptyState({ children, className, testId }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground',
        className,
      )}
      data-testid={testId}
    >
      {children}
    </div>
  )
}
