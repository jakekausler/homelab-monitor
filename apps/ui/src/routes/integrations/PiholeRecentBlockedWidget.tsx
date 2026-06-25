import type { JSX } from 'react'

import { useRecentBlocked } from '@/api/pihole'
import { EmptyState } from '@/components/EmptyState'

import { QueryState } from './QueryState'

export function PiholeRecentBlockedWidget(): JSX.Element {
  const result = useRecentBlocked()

  return (
    <div data-testid="pihole-recent-blocked-widget" className="text-sm">
      <QueryState
        result={result}
        unavailableLabel="Pi-hole recent-blocked temporarily unavailable"
        renderData={(data) => {
          if (data.rows.length === 0) {
            return (
              <EmptyState testId="pihole-recent-blocked-empty">
                No recently blocked domains
              </EmptyState>
            )
          }

          return (
            <ul className="divide-y divide-border">
              {data.rows.map((domain, index) => (
                <li key={`${domain}:${index}`} className="break-all font-mono px-2 py-1 text-sm">
                  {domain}
                </li>
              ))}
            </ul>
          )
        }}
      />
    </div>
  )
}
