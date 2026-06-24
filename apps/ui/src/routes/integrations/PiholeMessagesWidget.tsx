import type { JSX } from 'react'

import { useMessages } from '@/api/pihole'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'

type MessageRow = Schema<'PiholeMessageRow'>

function formatTimestamp(epochSeconds: number | null): string | null {
  if (epochSeconds === null) return null
  const d = new Date(epochSeconds * 1000)
  if (Number.isNaN(d.getTime())) return null
  // Pure: derived solely from the input epoch (no Date.now()).
  return d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
}

export function PiholeMessagesWidget(): JSX.Element {
  const result = useMessages()

  return (
    <div data-testid="pihole-messages-widget" className="text-sm">
      {result.isPending && <p className="text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Pi-hole messages temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}

      {result.data &&
        (result.data.rows.length === 0 ? (
          <EmptyState testId="pihole-messages-empty">No diagnostic messages</EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {result.data.rows.map((row: MessageRow) => {
              const ts = formatTimestamp(row.timestamp)
              return (
                <li key={row.id} className="space-y-1 py-2">
                  <div className="flex items-baseline justify-between gap-2">
                    <Badge variant="warn">{row.type}</Badge>
                    {ts !== null && (
                      <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
                        {ts}
                      </span>
                    )}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-muted-foreground">
                    {row.message}
                  </p>
                  {row.url !== null && row.url !== '' && /^https?:\/\//i.test(row.url) && (
                    <a
                      href={row.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-primary underline-offset-4 hover:underline break-all"
                    >
                      {row.url}
                    </a>
                  )}
                </li>
              )
            })}
          </ul>
        ))}
    </div>
  )
}
