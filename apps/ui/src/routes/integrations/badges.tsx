import {
  Play,
  Square,
  RefreshCw,
  Pause,
  Circle,
  HelpCircle,
  Heart,
  HeartCrack,
  HeartPulse,
} from 'lucide-react'
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

  const iconClass = 'size-3.5'
  let icon: React.ReactNode

  if (status === 'running') {
    icon = <Play className={`${iconClass} text-green-600 dark:text-green-400`} />
  } else if (status === 'exited' || status === 'dead') {
    icon = <Square className={`${iconClass} text-red-600 dark:text-red-400`} />
  } else if (status === 'restarting') {
    icon = (
      <RefreshCw className={`${iconClass} text-yellow-600 dark:text-yellow-400 animate-spin`} />
    )
  } else if (status === 'paused') {
    icon = <Pause className={`${iconClass} text-yellow-600 dark:text-yellow-400`} />
  } else if (status === 'created') {
    icon = <Circle className={`${iconClass} text-muted-foreground`} />
  } else {
    icon = <HelpCircle className={`${iconClass} text-muted-foreground`} />
  }

  return (
    <Badge
      variant={variant}
      aria-label={`Container status ${status}`}
      className="inline-flex gap-1"
    >
      {icon}
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

  const iconClass = 'size-3.5'
  let icon: React.ReactNode

  if (status === 'healthy') {
    icon = <Heart className={iconClass} />
  } else if (status === 'unhealthy') {
    icon = <HeartCrack className={iconClass} />
  } else if (status === 'starting') {
    icon = <HeartPulse className={iconClass} />
  } else {
    icon = null
  }

  return (
    <Badge
      variant={HEALTHCHECK_VARIANT[status]}
      aria-label={`Healthcheck ${status}`}
      className="inline-flex gap-1"
    >
      {icon}
      {titleCase(status)}
    </Badge>
  )
}
