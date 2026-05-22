import { Link } from '@tanstack/react-router'
import { useProbesSummary } from '@/api/docker'

interface ProbesBadgeProps {
  containerName: string
}

export function ProbesBadge({ containerName }: ProbesBadgeProps) {
  const { data, isPending } = useProbesSummary()
  if (isPending) return <span className="text-muted-foreground">—</span>
  const entry = data?.[containerName]
  if (!entry || entry.active === 0) return <span className="text-muted-foreground">—</span>
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
