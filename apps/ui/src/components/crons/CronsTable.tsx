import { Link } from '@tanstack/react-router'

import { Badge } from '@/components/ui/badge'
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
        {emptyHint ??
          'No crons yet. Crons will appear here once they are discovered or have registered a heartbeat.'}
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
              Wrapper
            </th>
            <th scope="col" className="px-3 py-2 text-left">
              Hidden
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((c) => (
            <tr
              key={c.fingerprint}
              className={
                c.soft_deleted_at !== null ? 'opacity-60 hover:bg-accent/30' : 'hover:bg-accent/30'
              }
            >
              <td className="px-3 py-2">
                <Link
                  to="/integrations/crons/$fingerprint"
                  params={{ fingerprint: c.fingerprint }}
                  className="font-medium text-primary hover:underline"
                >
                  {c.name}
                </Link>
              </td>
              <td className="px-3 py-2 text-muted-foreground">
                <span>{c.host}</span>
                {c.source_path === null && (
                  <Badge
                    variant="secondary"
                    className="ml-2"
                    aria-label="Remote cron (no disk source)"
                  >
                    Remote
                  </Badge>
                )}
              </td>
              <td className="px-3 py-2 font-mono text-xs">
                {c.schedule ?? `every ${String(c.cadence_seconds)}s`}
              </td>
              <td className="px-3 py-2">
                <StateBadge state={c.last_seen_state} />
                {c.soft_deleted_at !== null && (
                  <Badge
                    variant="muted"
                    className="ml-2 border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-300"
                    aria-label="Soft-deleted (no longer found on disk)"
                    data-testid="soft-deleted-badge"
                  >
                    Soft-deleted
                  </Badge>
                )}
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground">
                {formatRelative(c.last_ok_at)}
              </td>
              <td className="px-3 py-2 text-xs" data-testid="wrapper-cell">
                {c.wrapper_installed ? (
                  <Badge variant="secondary" aria-label="Wrapper installed">
                    ✓
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="px-3 py-2 text-xs">
                {c.hidden_at !== null && <Badge variant="secondary">Hidden</Badge>}
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
