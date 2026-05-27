import { Download, AlertTriangle, CheckCircle2 } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { useImageUpdatesSummary } from '@/api/docker'
import { formatDigest } from '@/lib/digest'
import { formatSourceHash } from '@/lib/sourceHash'

interface ImageUpdateBadgeProps {
  containerName: string
}

export function ImageUpdateBadge({ containerName }: ImageUpdateBadgeProps) {
  const { data, isPending, isError } = useImageUpdatesSummary()
  if (isPending) return null
  if (isError) {
    return null
  }
  const entry = data?.byContainer[containerName]
  if (!entry) return null

  if (entry.available) {
    const isLocalBuild = entry.source === 'local_build'
    const labelText = isLocalBuild ? 'Rebuild needed' : 'Update available'
    const ariaLabel = isLocalBuild
      ? `Source changed — rebuild needed for ${containerName}`
      : `Update available for ${containerName}`
    const titleText = isLocalBuild
      ? `Source hash: ${formatSourceHash(entry.last_source_hash)}`
      : `Current: ${formatDigest(entry.current_digest)} → Latest: ${formatDigest(entry.latest_digest)}`

    return (
      <Badge variant="warn" aria-label={ariaLabel} title={titleText} className="inline-flex gap-1">
        <Download className="size-3.5" />
        {labelText}
      </Badge>
    )
  }

  if (entry.check_error_reason) {
    return (
      <Badge
        variant="critical"
        aria-label="Update Check Failed"
        title={`Last check failed: ${entry.check_error_reason}${entry.check_failed_at ? ` at ${entry.check_failed_at}` : ''}`}
        className="inline-flex gap-1"
      >
        <AlertTriangle className="size-3.5" />
        Update Check Failed
      </Badge>
    )
  }

  return (
    <Badge
      variant="ok"
      aria-label="Image up to date"
      title="Up to date"
      className="inline-flex gap-1"
    >
      <CheckCircle2 className="size-3.5" />
      Up to date
    </Badge>
  )
}
