import type { JSX } from 'react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { formatRelative } from '@/lib/relativeTime'
import { useNowTick } from '@/lib/useNowTick'
import type { RunFeedback } from '@/api/autofix-runs'

interface Props {
  items: RunFeedback[]
}

export function FeedbackList({ items }: Props): JSX.Element {
  const nowMs = useNowTick(1000)
  if (items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="feedback-empty">
        No feedback items for this run.
      </p>
    )
  }
  return (
    <div className="space-y-3">
      {items.map((f) => (
        <FeedbackItem key={f.id} f={f} nowMs={nowMs} />
      ))}
    </div>
  )
}

function FeedbackItem({ f, nowMs }: { f: RunFeedback; nowMs: number }): JSX.Element {
  const [expanded, setExpanded] = useState(false)
  return (
    <Card data-testid={`feedback-item-${f.id}`}>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <FeedbackKindBadge kind={f.kind} />
        <span className="text-xs text-muted-foreground">{formatRelative(f.created_at, nowMs)}</span>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <p className="whitespace-pre-wrap">{f.suggestion_text}</p>
        {f.structured_hint !== null && (
          <div>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => setExpanded((v) => !v)}
              data-testid={`feedback-hint-toggle-${f.id}`}
            >
              {expanded ? 'Hide structured hint' : 'Show structured hint'}
            </Button>
            {expanded && (
              <pre
                className="mt-2 overflow-x-auto rounded bg-muted p-2 font-mono text-xs max-h-[400px] overflow-y-auto"
                data-testid={`feedback-hint-body-${f.id}`}
              >
                {JSON.stringify(f.structured_hint, null, 2)}
              </pre>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function FeedbackKindBadge({ kind }: { kind: RunFeedback['kind'] }): JSX.Element {
  if (kind === 'parse_error') {
    return (
      <Badge variant="critical" data-testid={`feedback-kind-${kind}`}>
        {kind}
      </Badge>
    )
  }
  return (
    <Badge variant="outline" data-testid={`feedback-kind-${kind}`}>
      {kind}
    </Badge>
  )
}
