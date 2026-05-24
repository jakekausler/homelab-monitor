import { useImageUpdatesSummary } from '@/api/docker'

export function RateLimitBanner() {
  const { data } = useImageUpdatesSummary()
  if (!data || data.rateLimitSkippedCount === 0) return null

  return (
    <div
      data-testid="image-update-rate-limit-banner"
      role="status"
      aria-live="polite"
      className="rounded-md border border-yellow-300 bg-yellow-50 px-3 py-2 text-xs text-yellow-900"
    >
      {data.rateLimitSkippedCount} container{data.rateLimitSkippedCount === 1 ? '' : 's'} skipped
      due to Docker Hub rate limit; will retry next tick.
    </div>
  )
}
