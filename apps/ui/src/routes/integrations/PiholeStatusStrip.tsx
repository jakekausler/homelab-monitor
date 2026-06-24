import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'
import { Badge } from '@/components/ui/badge'
import { usePiholeOverview } from '@/api/pihole'
import { useReenableCountdown } from './useReenableCountdown'

/**
 * Format a non-negative integer number of seconds as "M:SS" (e.g. 298 → "4:58",
 * 60 → "1:00", 5 → "0:05", 0 → "0:00"). Returns "—" when `seconds` is null.
 */
export function formatMSS(seconds: number | null): string {
  if (seconds === null) return '—'
  const safe = Math.max(0, Math.floor(seconds))
  const minutes = Math.floor(safe / 60)
  const secs = safe % 60
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}

export function PiholeStatusStrip(): JSX.Element {
  const result = usePiholeOverview()

  const blockingEnabled = result.data?.blocking_enabled
  const blockingTimer = result.data?.blocking_timer_seconds

  const remaining = useReenableCountdown(blockingEnabled === false ? blockingTimer : null)

  return (
    <div data-testid="pihole-status-strip" className="px-4 pt-2">
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Pi-hole metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {result.data && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={result.data.up ? 'ok' : 'critical'}>
            Pi-hole {result.data.up ? 'up' : 'down'}
          </Badge>

          {result.data.blocking_enabled === true && <Badge variant="ok">Blocking on</Badge>}
          {result.data.blocking_enabled === false &&
            result.data.blocking_timer_seconds != null &&
            result.data.blocking_timer_seconds > 0 && (
              <Badge variant="warn">Blocking off · re-enables in {formatMSS(remaining)}</Badge>
            )}
          {result.data.blocking_enabled === false &&
            (result.data.blocking_timer_seconds == null ||
              result.data.blocking_timer_seconds <= 0) && (
              <Badge variant="critical">Blocking off</Badge>
            )}
          {result.data.blocking_enabled === null && <Badge variant="muted">Blocking —</Badge>}

          <span className="text-muted-foreground">
            {result.data.percent_blocked != null
              ? `${result.data.percent_blocked.toFixed(1)}%`
              : '—%'}{' '}
            blocked
          </span>

          <span className="text-muted-foreground">
            {result.data.query_frequency != null ? result.data.query_frequency.toFixed(1) : '—'} q/s
          </span>

          {result.data.messages_count > 0 ? (
            <Badge variant="critical">{result.data.messages_count} messages</Badge>
          ) : (
            <span className="text-muted-foreground">No messages</span>
          )}
        </div>
      )}
    </div>
  )
}
