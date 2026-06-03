import { Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { formatRelative } from '@/lib/relativeTime'
import { useQueryHistory } from '@/lib/useQueryHistory'
import type { HistoryEntry } from '@/lib/queryHistory'

interface QueryHistoryPanelProps {
  /** Click a row to load it into the Explorer (page reconstructs state). */
  onLoad: (entry: HistoryEntry) => void
}

/** A compact one-line preview of the query + its services/range context. */
function entryPreview(entry: HistoryEntry): string {
  const expr = entry.logs_ql.trim()
  const exprLabel = expr.length > 0 ? expr : entry.advanced_mode ? '*' : '(all)'
  const services =
    entry.selected_services.length > 0
      ? entry.selected_services.map((s) => `${s.source_type}:${s.service}`).join(', ')
      : null
  const range =
    entry.since_preset != null
      ? entry.since_preset
      : entry.range_start_iso != null
        ? 'custom range'
        : null
  const parts = [exprLabel]
  if (services != null) parts.push(`[${services}]`)
  if (range != null) parts.push(`· ${range}`)
  return parts.join(' ')
}

export function QueryHistoryPanel({ onLoad }: QueryHistoryPanelProps) {
  const { entries, clear } = useQueryHistory()

  if (entries.length === 0) {
    return (
      <div className="space-y-2 p-4" data-testid="logs-history-empty">
        <p className="text-sm text-muted-foreground">
          No recent queries yet. Run a search to populate.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2" data-testid="logs-history-panel">
      <div className="flex justify-end">
        <Button
          size="sm"
          variant="ghost"
          data-testid="logs-history-clear"
          onClick={clear}
          className="h-7 text-xs"
        >
          <Trash2 className="size-3 mr-1" />
          Clear history
        </Button>
      </div>
      <div className="space-y-2 overflow-y-auto max-h-96 border rounded-md p-2">
        {entries.map((entry) => (
          <button
            key={entry.id}
            type="button"
            data-testid="logs-history-row"
            onClick={() => onLoad(entry)}
            className="flex w-full min-w-0 flex-col gap-0.5 rounded-md border border-transparent p-2 text-left hover:bg-muted"
          >
            <span className="text-xs text-muted-foreground">
              {formatRelative(new Date(entry.timestamp).toISOString())}
            </span>
            <span className="min-w-0 truncate text-sm" title={entryPreview(entry)}>
              {entryPreview(entry)}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
