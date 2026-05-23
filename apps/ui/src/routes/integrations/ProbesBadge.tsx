import { Link } from '@tanstack/react-router'
import { useProbesSummary } from '@/api/docker'

interface ProbesBadgeProps {
  containerName: string
}

export function ProbesBadge({ containerName }: ProbesBadgeProps) {
  const { data, isPending } = useProbesSummary()
  if (isPending) return <span className="text-muted-foreground">—</span>
  const entry = data?.[containerName]
  if (!entry) return <span className="text-muted-foreground">—</span>

  // STAGE-003-007: surface override-file validation errors as a red badge.
  if (entry.config_errors && entry.config_errors.length > 0) {
    return (
      <Link
        to="/integrations/docker/containers/$name/probes"
        params={{ name: containerName }}
        className="rounded bg-red-50 px-2 py-0.5 text-xs text-red-800 hover:underline"
        aria-label={`Config error for ${containerName}: ${entry.config_errors.join('; ')}`}
        title={`${entry.config_errors.length} validation error${entry.config_errors.length === 1 ? '' : 's'} — click to view`}
      >
        Config error
      </Link>
    )
  }

  if (entry.active === 0) return <span className="text-muted-foreground">—</span>

  const text =
    entry.failing > 0
      ? `${entry.active} active, ${entry.failing} failing`
      : `${entry.active} active`
  return (
    <Link
      to="/integrations/docker/containers/$name/probes"
      params={{ name: containerName }}
      className="text-xs hover:underline"
      aria-label={`View probes for ${containerName}: ${entry.active} active${entry.failing > 0 ? `, ${entry.failing} failing` : ''}`}
    >
      {text}
    </Link>
  )
}
