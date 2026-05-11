import { Badge } from '@/components/ui/badge'

const MODE_VARIANT = {
  observe: 'muted',
  heartbeat: 'default',
  both: 'secondary',
} as const

export function ModeBadge({ mode }: { mode: 'observe' | 'heartbeat' | 'both' }) {
  return (
    <Badge variant={MODE_VARIANT[mode]} aria-label={`Integration mode ${mode}`}>
      {mode}
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
      {state}
    </Badge>
  )
}
