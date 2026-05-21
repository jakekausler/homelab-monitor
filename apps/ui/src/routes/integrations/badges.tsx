import { Badge } from '@/components/ui/badge'
import { capitalize as titleCase } from '@/lib/text'

// All possible Docker State.Status values (per Docker Engine API):
// created, running, paused, restarting, removing, exited, dead.
// Plus our synthetic 'missing' state for containers that disappeared.
const STATUS_VARIANT = {
  running: 'ok',
  created: 'muted',
  exited: 'critical',
  restarting: 'warn',
  paused: 'muted',
  removing: 'muted',
  dead: 'critical',
  missing: 'muted',
} as const

type ContainerStatus = keyof typeof STATUS_VARIANT

function isKnownStatus(s: string): s is ContainerStatus {
  return s in STATUS_VARIANT
}

export function StatusBadge({ status }: { status: string }) {
  const variant = isKnownStatus(status) ? STATUS_VARIANT[status] : 'muted'
  return (
    <Badge variant={variant} aria-label={`Container status ${status}`}>
      {titleCase(status)}
    </Badge>
  )
}

const HEALTHCHECK_VARIANT = {
  healthy: 'ok',
  unhealthy: 'critical',
  starting: 'warn',
} as const

export type HealthcheckStatus = keyof typeof HEALTHCHECK_VARIANT

function isHealthcheckStatus(s: string): s is HealthcheckStatus {
  return s === 'healthy' || s === 'unhealthy' || s === 'starting'
}

export function HealthcheckBadge({ status }: { status: string | null | undefined }) {
  if (!status || !isHealthcheckStatus(status)) return null
  return (
    <Badge variant={HEALTHCHECK_VARIANT[status]} aria-label={`Healthcheck ${status}`}>
      {titleCase(status)}
    </Badge>
  )
}

export function RestartCountBadge({ count }: { count: number }) {
  if (count === 0) {
    return <span className="text-muted-foreground">0</span>
  }
  const variant = count >= 3 ? 'critical' : 'warn'
  return (
    <Badge variant={variant} aria-label={`Restart count ${count}`}>
      {count}
    </Badge>
  )
}
