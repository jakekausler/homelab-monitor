import { AlertCircle, Activity } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { useProbesSummary } from '@/api/docker'

interface ProbesBadgeProps {
  containerName: string
}

export function ProbesBadge({ containerName }: ProbesBadgeProps) {
  const { data, isPending } = useProbesSummary()
  if (isPending) return null
  const entry = data?.[containerName]
  if (!entry) return null

  // STAGE-003-007: surface override-file validation errors as a red badge.
  if (entry.config_errors && entry.config_errors.length > 0) {
    return (
      <Badge
        variant="critical"
        aria-label={`Config error for ${containerName}: ${entry.config_errors.join('; ')}`}
        title={`${entry.config_errors.length} validation error${entry.config_errors.length === 1 ? '' : 's'}`}
        className="inline-flex gap-1"
      >
        <AlertCircle className="size-3.5" />
        Config errors
      </Badge>
    )
  }

  if (entry.active === 0) return null

  const isFailing = entry.failing > 0
  const variant = isFailing ? 'critical' : 'ok'
  const text = isFailing ? `${entry.failing} failing` : `${entry.active} active`
  const ariaLabel = `Probes for ${containerName}: ${entry.active} active${isFailing ? `, ${entry.failing} failing` : ''}`

  return (
    <Badge variant={variant} aria-label={ariaLabel} className="inline-flex gap-1">
      <Activity className="size-3.5" />
      {text}
    </Badge>
  )
}
