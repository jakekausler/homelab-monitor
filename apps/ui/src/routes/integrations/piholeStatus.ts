import type { VariantProps } from 'class-variance-authority'

import type { badgeVariants } from '@/components/ui/badge'

type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>['variant']>

/**
 * Maps a Pi-hole adlist `status` string to a Badge variant.
 * - 'ok'              -> 'ok'
 * - contains fail/error -> 'critical'
 * - '' or 'unknown'   -> 'muted'
 * - anything else     -> 'warn'
 */
export function adlistStatusToBadgeVariant(status: string): BadgeVariant {
  const s = status.trim().toLowerCase()
  if (s === 'ok') return 'ok'
  if (s.includes('fail') || s.includes('error')) return 'critical'
  if (s === '' || s === 'unknown') return 'muted'
  return 'warn'
}
