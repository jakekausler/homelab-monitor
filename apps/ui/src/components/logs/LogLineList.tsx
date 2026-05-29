import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'
import { formatLogTimestamp } from '@/lib/relativeTime'
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
}

export function LogLineList({ lines, emptyContent, testId, wrap = false }: LogLineListProps) {
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
        : lines.map((line, i) => (
            <LogRow key={`${line.timestamp}-${String(i)}`} line={line} wrap={wrap} />
          ))}
    </pre>
  )
}

function LogRow({ line, wrap }: { line: LogLine; wrap: boolean }) {
  const tint = severityTintClass(line.severity)
  const segments = parseAnsi(line.message)
  return (
    <div className="grid grid-cols-[auto_1fr] items-start gap-x-2">
      <span className="whitespace-nowrap border-r border-border pr-2 text-muted-foreground">
        {formatLogTimestamp(line.timestamp)}
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
