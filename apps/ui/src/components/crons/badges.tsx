import { Badge } from '@/components/ui/badge'
import { capitalize as titleCase } from '@/lib/text'

const STATE_VARIANT = {
  unknown: 'muted',
  running: 'default',
  ok: 'ok',
  failed: 'critical',
  late: 'warn',
} as const

export function StateBadge({ state }: { state: 'unknown' | 'running' | 'ok' | 'failed' | 'late' }) {
  return (
    <Badge variant={STATE_VARIANT[state]} aria-label={`Last seen state ${state}`}>
      {titleCase(state)}
    </Badge>
  )
}

// Re-exported for backward-compatible imports from the crons module.
export { titleCase }
