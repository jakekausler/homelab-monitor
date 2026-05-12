import { Link } from '@tanstack/react-router'

import { StateBadge } from '@/components/crons/badges'
import { formatRelative } from '@/lib/relativeTime'
import type { Schema } from '@/api/types'

type CronOut = Schema<'CronOut'>

export interface CronsTableProps {
  items: CronOut[]
  isLoading: boolean
  emptyHint?: string
}

export function CronsTable({ items, isLoading, emptyHint }: CronsTableProps) {
  if (isLoading) {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        Loading crons…
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        {emptyHint ?? 'No crons yet. Click "Add cron" to register your first.'}
      </div>
    )
  }
  return (
    <div className="overflow-x-auto rounded-md border border-border bg-card">
      <table className="min-w-full divide-y divide-border text-sm">
        <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left">
              Name
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Host
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Schedule
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              State
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Last OK
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Enabled
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((c) => (
            <tr key={c.fingerprint} className="hover:bg-accent/30">
              <td className="px-3 py-2">
                <Link
                  to="/inventory/crons/$fingerprint"
                  params={{ fingerprint: c.fingerprint }}
                  className="font-medium text-primary hover:underline"
                >
                  {c.name}
                </Link>
              </td>
              <td className="px-3 py-2 text-muted-foreground">{c.host}</td>
              <td className="px-3 py-2 font-mono text-xs">
                {c.schedule ?? `every ${String(c.cadence_seconds)}s`}
              </td>
              <td className="px-3 py-2">
                <StateBadge state={c.last_seen_state} />
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground">—</td>
              <td className="px-3 py-2 text-xs">
                {c.enabled ? 'Yes' : 'No'}
                {c.hidden_at !== null && (
                  <span className="ml-2 rounded bg-muted px-1 text-muted-foreground">archived</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// formatRelative re-exported for tests that want to verify column formatting.
export { formatRelative }
