import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import type { LogBannerTone } from './types'

interface LogBannerProps {
  tone: LogBannerTone
  children: ReactNode
  testId?: string
  role?: 'status' | 'alert'
}

const TONE_CLASSES: Record<LogBannerTone, string> = {
  amber: 'border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-200',
  blue: 'border-blue-500/30 bg-blue-500/10 text-blue-900 dark:text-blue-200',
}

/** Small rounded banner (p-2 text-xs) used for truncated / running notices. */
export function LogBanner({ tone, children, testId, role }: LogBannerProps) {
  return (
    <p
      className={cn('rounded-md border p-2 text-xs', TONE_CLASSES[tone])}
      data-testid={testId}
      role={role}
    >
      {children}
    </p>
  )
}
