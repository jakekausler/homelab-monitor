import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import { formatLogTimestampParts } from '@/lib/relativeTime'
import { parseAnsi } from './ansi'
import { severityTintClass } from './severity'
import type { LogLine } from './types'

/**
 * STAGE-004-018B — resolve one cell value for a configured column.
 * PURE. Promoted top-level fields (severity/host/service) take precedence over
 * the `fields` bag. Missing → ''. Non-string values are coerced via String().
 */
export function getColumnValue(line: LogLine, field: string): string {
  // Promoted top-level fields first.
  if (field === 'severity') return line.severity ?? ''
  if (field === 'host') return line.host ?? ''
  if (field === 'service') return line.service ?? ''
  // Then the fields bag.
  const v = line.fields[field]
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return JSON.stringify(v)
}

interface LogLineListProps {
  lines: LogLine[]
  /** Rendered inside the <pre> when lines is empty. */
  emptyContent?: ReactNode
  /** data-testid on the <pre> (e.g. 'logs-body' for docker, 'log-body' for cron). */
  testId?: string
  /** Soft-wrap long lines instead of horizontal scroll. Default false. */
  wrap?: boolean
  /** STAGE-004-009: UTC vs configured-local timestamps. Default 'local'. */
  timezone?: 'local' | 'utc'
  /** STAGE-004-016: invoked with the clicked line + its key when the field
   *  inspector is enabled. Absent → rows are not clickable (zero behavior change). */
  onLineClick?: (line: LogLine, key: string) => void
  /** STAGE-004-016: key of the currently-inspected line (for highlight). */
  selectedKey?: string | null
  /** STAGE-004-018B: ordered list of extra column field names. When non-empty,
   *  rows render via LogRowWithColumns (timestamp | one cell per column | message).
   *  Absent/empty → the existing 2-column LogRow renders (back-compat). */
  columns?: string[] | undefined
}

export function LogLineList({
  lines,
  emptyContent,
  testId,
  wrap = false,
  timezone = 'local',
  onLineClick,
  selectedKey,
  columns,
}: LogLineListProps) {
  const useColumns = columns !== undefined && columns.length > 0
  // timestamp (auto) | one auto col per configured column | message (1fr)
  const gridTemplateColumns = useColumns ? `auto ${'auto '.repeat(columns.length)}1fr` : ''

  return (
    <pre
      className={cn(
        'rounded-md border border-border bg-muted/30 p-3 text-xs font-mono',
        wrap ? 'whitespace-normal' : 'overflow-x-auto',
      )}
      data-testid={testId}
    >
      {lines.length === 0
        ? emptyContent
        : lines.map((line, i) => {
            // STAGE-004-016: index-based key. NOTE: on "Load older", prepended
            // older lines shift indices, so the inspected highlight may drift to
            // a different line. Accepted — no special handling (D-016).
            const key = `${line.timestamp}-${String(i)}`
            return useColumns ? (
              <LogRowWithColumns
                key={key}
                line={line}
                wrap={wrap}
                timezone={timezone}
                lineKey={key}
                columns={columns}
                gridTemplateColumns={gridTemplateColumns}
                {...(onLineClick !== undefined && { onLineClick })}
                isSelected={selectedKey === key}
              />
            ) : (
              <LogRow
                key={key}
                line={line}
                wrap={wrap}
                timezone={timezone}
                lineKey={key}
                {...(onLineClick !== undefined && { onLineClick })}
                isSelected={selectedKey === key}
              />
            )
          })}
    </pre>
  )
}

function LogRow({
  line,
  wrap,
  timezone,
  lineKey,
  onLineClick,
  isSelected = false,
}: {
  line: LogLine
  wrap: boolean
  timezone: 'local' | 'utc'
  lineKey: string
  onLineClick?: (line: LogLine, key: string) => void
  isSelected?: boolean
}) {
  const tint = severityTintClass(line.severity)
  const segments = parseAnsi(line.message)
  // STAGE-004-009: `display` is the chosen zone; `title` shows the OTHER zone.
  const ts = formatLogTimestampParts(line.timestamp, { timezone })
  const clickable = onLineClick !== undefined
  return (
    <div
      className={cn(
        // When NOT wrapping, min-w-max sizes the row to its content width so the
        // hover/selected background + ring span the FULL line (not just the
        // viewport) inside the horizontally-scrollable container. When wrapping,
        // min-w-max must be OMITTED: it would force the row to its intrinsic
        // content width and defeat wrapping, overflowing narrow (mobile)
        // containers horizontally instead of flowing onto multiple lines.
        'grid grid-cols-[auto_1fr] items-start gap-x-2',
        !wrap && 'min-w-max',
        clickable && 'cursor-pointer rounded-sm hover:bg-accent/40',
        isSelected && 'bg-accent ring-1 ring-ring',
      )}
      {...(clickable && {
        role: 'button',
        tabIndex: 0,
        'aria-pressed': isSelected,
        'data-testid': `log-row${isSelected ? '-selected' : ''}`,
        onClick: () => onLineClick(line, lineKey),
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onLineClick(line, lineKey)
          }
        },
      })}
    >
      <span
        className="whitespace-nowrap border-r border-border pr-2 text-muted-foreground"
        title={ts.tooltip}
      >
        {ts.display}
      </span>
      <span className={cn(wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre')}>
        {segments.map((seg, j) => (
          <span
            key={j}
            className={cn(
              // ansi.ts emits a text-* class iff an explicit fg color is set, so
              // 'text-' presence means ANSI fg wins; otherwise the severity tint
              // is the fg fallback. Keep classesFor() honoring that invariant.
              seg.classes.includes('text-') ? seg.classes : cn(tint, seg.classes),
            )}
          >
            {seg.text}
          </span>
        ))}
      </span>
    </div>
  )
}

function LogRowWithColumns({
  line,
  wrap,
  timezone,
  lineKey,
  columns,
  gridTemplateColumns,
  onLineClick,
  isSelected = false,
}: {
  line: LogLine
  wrap: boolean
  timezone: 'local' | 'utc'
  lineKey: string
  columns: string[]
  gridTemplateColumns: string
  onLineClick?: (line: LogLine, key: string) => void
  isSelected?: boolean
}) {
  const tint = severityTintClass(line.severity)
  const segments = parseAnsi(line.message)
  const ts = formatLogTimestampParts(line.timestamp, { timezone })
  const clickable = onLineClick !== undefined
  return (
    <div
      className={cn(
        'grid items-start gap-x-2',
        !wrap && 'min-w-max',
        clickable && 'cursor-pointer rounded-sm hover:bg-accent/40',
        isSelected && 'bg-accent ring-1 ring-ring',
      )}
      style={{ gridTemplateColumns }}
      data-testid="log-row-columns"
      {...(clickable && {
        role: 'button',
        tabIndex: 0,
        'aria-pressed': isSelected,
        onClick: () => onLineClick(line, lineKey),
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onLineClick(line, lineKey)
          }
        },
      })}
    >
      <span
        className="whitespace-nowrap border-r border-border pr-2 text-muted-foreground"
        title={ts.tooltip}
      >
        {ts.display}
      </span>
      {columns.map((field) => (
        <span
          key={field}
          data-testid="log-cell"
          data-field={field}
          className={cn(
            'whitespace-nowrap border-r border-border pr-2',
            field === 'severity' && tint,
          )}
        >
          {getColumnValue(line, field)}
        </span>
      ))}
      <span className={cn(wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre')}>
        {segments.map((seg, j) => (
          <span
            key={j}
            className={cn(seg.classes.includes('text-') ? seg.classes : cn(tint, seg.classes))}
          >
            {seg.text}
          </span>
        ))}
      </span>
    </div>
  )
}
