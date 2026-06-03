import { Copy, Plus, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useCopyToClipboard } from '@/lib/useCopyToClipboard'
import type { LogLine } from './types'

interface FieldInspectorPanelProps {
  line: LogLine
  onClose: () => void
  onAddServiceFilter?: (service: string, sourceType: string) => void
  onAddMsgFilter?: (value: string) => void
  onAddFieldFilter?: (field: string, value: string) => void
}

/** One normalized row: the display label, the display value, and the raw
 *  string value used for Copy / Add-to-filter. */
interface FieldRow {
  name: string
  /** Display text — '—' for empty string. */
  display: string
  /** Raw value used for clipboard + filter; null when there's nothing to act on. */
  value: string | null
  /** False when the raw value was null/undefined/empty-string (row omitted).
   *  True only for values that have meaningful content. */
  present: boolean
}

function toRow(name: string, raw: unknown): FieldRow {
  if (raw == null || (typeof raw === 'string' && raw.trim() === '')) {
    return { name, display: '—', value: null, present: false }
  }
  // eslint-disable-next-line @typescript-eslint/no-base-to-string
  const s = String(raw)
  return {
    name,
    display: s,
    value: s,
    present: true,
  }
}

export function FieldInspectorPanel({
  line,
  onClose,
  onAddServiceFilter,
  onAddMsgFilter,
  onAddFieldFilter,
}: FieldInspectorPanelProps) {
  const copy = useCopyToClipboard()

  const coreRows: FieldRow[] = [
    toRow('timestamp', line.timestamp),
    toRow('severity', line.severity),
    toRow('service', line.service),
    toRow('host', line.host),
    toRow('stream', line.stream),
    toRow('message', line.message),
  ].filter((r) => r.present)
  // Bag entries, alphabetical by key. Absent (null/undefined) entries omitted.
  const bagRows: FieldRow[] = Object.keys(line.fields)
    .sort((a, b) => a.localeCompare(b))
    .map((k) => toRow(k, line.fields[k]))
    .filter((r) => r.present)

  // eslint-disable-next-line @typescript-eslint/no-base-to-string
  const sourceType = String(line.fields['source_type'] ?? 'unknown')

  // Decide the add-to-filter handler for a given row (undefined => no button).
  // Routing:
  //   timestamp          → never (copy-only)
  //   service            → onAddServiceFilter (identity chip)
  //   host, severity,
  //   bag entries        → onAddFieldFilter(fieldName, value) [structured LogsQL]
  //   message, stream    → onAddMsgFilter(value) [substring / _msg:"..."]
  //     (stream maps to VL _stream_id builtin, not a queryable flat field,
  //      so _msg substring fallback is the safest filter for it)
  const addHandlerFor = (row: FieldRow): (() => void) | undefined => {
    if (row.name === 'timestamp' || row.value === null) return undefined
    if (row.name === 'service') {
      if (onAddServiceFilter === undefined) return undefined
      const value = row.value
      return () => onAddServiceFilter(value, sourceType)
    }
    if (row.name === 'message' || row.name === 'stream') {
      if (onAddMsgFilter === undefined) return undefined
      const value = row.value
      return () => onAddMsgFilter(value)
    }
    if (row.name === 'severity') {
      if (onAddFieldFilter === undefined) return undefined
      // Use the raw stored severity token (e.g. "4", "WARNING") rather than
      // the normalized display value ("warn", "info") — VL indexes the raw value.
      // severity_raw is absent only when VL had no severity field at all, which
      // in practice never happens (vector always writes one); the fallback to
      // row.value is a dead-code safety net.
      // eslint-disable-next-line @typescript-eslint/no-base-to-string -- line.fields is Record<string, unknown>
      const rawSeverity = String(line.fields['severity_raw'] ?? row.value)
      return () => onAddFieldFilter('severity', rawSeverity)
    }
    // severity_raw is a backend-only field (not stored in VictoriaLogs as a queryable field);
    // filtering on it matches nothing. The 'severity' core row filters correctly. Copy stays available.
    if (row.name === 'severity_raw') return undefined
    // host and all bag entries → structured field filter (display value = stored value)
    if (onAddFieldFilter === undefined) return undefined
    const field = row.name
    const value = row.value
    return () => onAddFieldFilter(field, value)
  }

  const renderRow = (row: FieldRow) => {
    const onAdd = addHandlerFor(row)
    return (
      <div
        key={row.name}
        data-testid={`field-row-${row.name}`}
        className="flex items-start gap-2 border-b border-border/60 py-1.5 last:border-b-0"
      >
        <span
          className="w-24 shrink-0 truncate font-mono text-xs text-muted-foreground"
          title={row.name}
        >
          {row.name}
        </span>
        <span className="min-w-0 flex-1 break-all font-mono text-xs">{row.display}</span>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="h-6 w-6"
            data-testid={`field-copy-${row.name}`}
            aria-label={`Copy ${row.name}`}
            disabled={row.value === null}
            onClick={() => {
              if (row.value !== null) void copy(row.value, row.name)
            }}
          >
            <Copy className="h-3 w-3" />
          </Button>
          {onAdd !== undefined && (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="h-6 w-6"
              data-testid={`field-add-filter-${row.name}`}
              aria-label={`Add ${row.name} to filter`}
              onClick={onAdd}
            >
              <Plus className="h-3 w-3" />
            </Button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div data-testid="field-inspector-panel" className="flex h-full flex-col">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold">Field inspector</h2>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="h-7 w-7"
          data-testid="field-inspector-close"
          aria-label="Close field inspector"
          onClick={onClose}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {coreRows.map(renderRow)}
        {bagRows.map(renderRow)}
      </div>
    </div>
  )
}
