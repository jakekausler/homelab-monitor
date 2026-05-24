import { Link } from '@tanstack/react-router'
import { useImageUpdatesSummary } from '@/api/docker'
import { formatDigest } from '@/lib/digest'
import { formatSourceHash } from '@/lib/sourceHash'

interface ImageUpdateBadgeProps {
  containerName: string
}

export function ImageUpdateBadge({ containerName }: ImageUpdateBadgeProps) {
  const { data, isPending, isError, error } = useImageUpdatesSummary()
  if (isPending) return <span className="text-muted-foreground">—</span>
  if (isError) {
    return (
      <span
        className="text-xs text-red-600 cursor-help"
        title={`Failed to load image update status: ${error instanceof Error ? error.message : 'unknown error'}`}
      >
        ?
      </span>
    )
  }
  const entry = data?.byContainer[containerName]
  if (!entry) return <span className="text-muted-foreground">—</span>

  if (entry.available) {
    const isLocalBuild = entry.source === 'local_build'
    const labelText = isLocalBuild ? 'Source changed — rebuild needed' : 'Update available'
    const ariaLabel = isLocalBuild
      ? `Source changed — rebuild needed for ${containerName}`
      : `Update available for ${containerName}`
    const titleText = isLocalBuild
      ? `Source hash: ${formatSourceHash(entry.last_source_hash)}\nClick for full details`
      : `Current: ${formatDigest(entry.current_digest)} → Latest: ${formatDigest(entry.latest_digest)}\nClick for full details`

    return (
      <Link
        to="/integrations/docker/containers/$name/image-update"
        params={{ name: containerName }}
        className="rounded bg-blue-50 px-2 py-0.5 text-xs text-blue-800 hover:underline"
        aria-label={ariaLabel}
        title={titleText}
      >
        {labelText}
      </Link>
    )
  }

  if (entry.check_error_reason) {
    return (
      <span
        className="text-xs text-muted-foreground"
        title={`Last check failed: ${entry.check_error_reason}${entry.check_failed_at ? ` at ${entry.check_failed_at}` : ''}`}
      >
        check failed
      </span>
    )
  }

  return (
    <span className="text-xs text-muted-foreground" title="Up to date">
      up to date
    </span>
  )
}
