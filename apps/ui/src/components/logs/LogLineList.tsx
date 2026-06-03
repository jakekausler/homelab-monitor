import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import { formatLogTimestampParts } from '@/lib/relativeTime'
import { parseAnsi } from './ansi'
import { severityTintClass } from './severity'
import type { LogLine } from './types'

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
}

export function LogLineList({
  lines,
  emptyContent,
  testId,
  wrap = false,
  timezone = 'local',
  onLineClick,
  selectedKey,
}: LogLineListProps) {
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
            return (
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
        'grid grid-cols-[auto_1fr] items-start gap-x-2',
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
