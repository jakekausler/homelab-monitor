import { Badge } from '@/components/ui/badge'
import { capitalize as titleCase } from '@/lib/text'

const MODE_VARIANT = {
  observe: 'muted',
  heartbeat: 'default',
  both: 'secondary',
} as const

export function ModeBadge({ mode }: { mode: 'observe' | 'heartbeat' | 'both' }) {
  return (
    <Badge variant={MODE_VARIANT[mode]} aria-label={`Integration mode ${mode}`}>
      {titleCase(mode)}
    </Badge>
  )
}

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
