import { useState } from 'react'

import { useListComposeActions } from '@/api/docker'
import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { formatRelative } from '@/lib/relativeTime'

interface RecentActionsPanelProps {
  containerName: string
}

const STATE_COLORS: Record<string, string> = {
  running: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-100',
  pulling: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-100',
  building: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-100',
  restarting: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-100',
  success: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-100',
  failed: 'bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-100',
  timeout: 'bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-100',
  killed: 'bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-100',
}

export function RecentActionsPanel({ containerName }: RecentActionsPanelProps) {
  const result = useListComposeActions(containerName, 10)

  return (
    <div className="space-y-2">
      <h2 className="text-base font-semibold tracking-tight">Recent actions</h2>
      {result.isError && result.error && <ErrorDisplay error={result.error} />}
      {result.isPending && (
        <div className="text-sm text-muted-foreground">Loading recent actions…</div>
      )}
      {result.data && result.data.actions.length === 0 && (
        <EmptyState>No recent actions.</EmptyState>
      )}
      {result.data && result.data.actions.length > 0 && (
        <ul className="space-y-2">
          {result.data.actions.map((a) => (
            <RecentActionRow key={a.action_id} action={a} />
          ))}
        </ul>
      )}
    </div>
  )
}

type Action = NonNullable<ReturnType<typeof useListComposeActions>['data']>['actions'][number]

function RecentActionRow({ action }: { action: Action }) {
  const [expanded, setExpanded] = useState(false)
  const stateClass = STATE_COLORS[action.state] ?? 'bg-muted text-muted-foreground'
  return (
    <li className="rounded-md border border-border bg-card p-3 text-sm">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 text-left"
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide ${stateClass}`}
          >
            {action.state}
          </span>
          <span className="font-medium">{action.container_name}</span>
          <span className="text-xs text-muted-foreground">{action.action}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {action.duration_seconds != null && <span>{action.duration_seconds.toFixed(1)}s</span>}
          <span title={action.started_at}>{formatRelative(action.started_at)}</span>
          <span>by {action.who}</span>
          <span aria-hidden>{expanded ? '▾' : '▸'}</span>
        </div>
      </button>
      {expanded && (
        <div className="mt-3 space-y-2">
          <div>
            <div className="text-xs uppercase tracking-wide text-muted-foreground">Command</div>
            <pre className="overflow-x-auto rounded bg-muted/40 p-2 font-mono text-xs">
              {action.command}
            </pre>
          </div>
          {action.error_reason && (
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">
                Error reason
              </div>
              <code className="text-rose-700 dark:text-rose-300">{action.error_reason}</code>
            </div>
          )}
          {action.stdout && (
            <details>
              <summary className="cursor-pointer text-xs uppercase tracking-wide text-muted-foreground">
                stdout
              </summary>
              <pre className="overflow-x-auto rounded bg-muted/40 p-2 font-mono text-xs">
                {action.stdout}
              </pre>
            </details>
          )}
          {action.stderr && (
            <details>
              <summary className="cursor-pointer text-xs uppercase tracking-wide text-muted-foreground">
                stderr
              </summary>
              <pre className="overflow-x-auto rounded bg-muted/40 p-2 font-mono text-xs">
                {action.stderr}
              </pre>
            </details>
          )}
        </div>
      )}
    </li>
  )
}
