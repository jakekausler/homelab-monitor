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

export type RunState = 'running' | 'ok' | 'fail' | 'unknown'

const RUN_STATE_VARIANT = {
  running: 'default',
  ok: 'ok',
  fail: 'critical',
  unknown: 'muted',
} as const

export function RunStateBadge({ state }: { state: RunState }) {
  return (
    <Badge variant={RUN_STATE_VARIANT[state]} aria-label={`Run state ${state}`}>
      {titleCase(state)}
    </Badge>
  )
}

// Re-exported for backward-compatible imports from the crons module.
export { titleCase }
