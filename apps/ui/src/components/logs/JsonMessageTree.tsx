import { useState, useMemo } from 'react'
import { ChevronDown, ChevronRight, Copy } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useCopyToClipboard } from '@/lib/useCopyToClipboard'
import { buildJsonModel, type JsonLeafKind, type JsonNode } from './jsonMessage'

interface JsonMessageTreeProps {
  value: unknown
}

/** Per-level indent in px; capped so deep trees don't push content off-screen. */
const INDENT_STEP = 12
const INDENT_MAX = 8 // levels; beyond this the indent stops growing
function indentFor(depth: number): number {
  return Math.min(depth, INDENT_MAX) * INDENT_STEP
}

export function JsonMessageTree({ value }: JsonMessageTreeProps) {
  const copy = useCopyToClipboard()
  // Build the capped model once per render of this value.
  const model = useMemo(() => buildJsonModel(value), [value])
  const pretty = useMemo(() => JSON.stringify(value, null, 2), [value])

  return (
    <div data-testid="json-message-tree" className="min-w-0 flex-1">
      <div className="mb-1 flex justify-end">
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="h-6 w-6"
          data-testid="json-message-copy"
          aria-label="Copy message"
          onClick={() => void copy(pretty, 'message')}
        >
          <Copy className="h-3 w-3" />
        </Button>
      </div>
      <NodeView node={model} depth={0} />
    </div>
  )
}

interface NodeViewProps {
  node: JsonNode
  depth: number
}

function NodeView({ node, depth }: NodeViewProps) {
  // Top level (depth 0) expanded by default; deeper branches collapsed.
  const [open, setOpen] = useState(depth === 0)
  const padding = indentFor(depth)

  if (node.type === 'leaf') {
    return (
      <div
        data-testid={`json-node-${node.path}`}
        className="flex items-start gap-1 font-mono text-xs"
        style={{ paddingLeft: padding }}
      >
        {node.label !== null && (
          <span className="shrink-0 text-muted-foreground">{node.label}:</span>
        )}
        <span className={cn('min-w-0 flex-1 break-all', leafClass(node.valueKind))}>
          {node.text}
        </span>
      </div>
    )
  }

  if (node.type === 'truncated') {
    const reasonText = node.reason === 'depth' ? '(depth limit)' : '(truncated)'
    return (
      <div
        data-testid={`json-node-${node.path}`}
        className="flex items-start gap-1 font-mono text-xs text-muted-foreground"
        style={{ paddingLeft: padding }}
      >
        {node.label !== null && <span className="shrink-0">{node.label}:</span>}
        <span className="min-w-0 flex-1 break-all italic">
          {reasonText} {node.preview}
        </span>
      </div>
    )
  }

  // branch
  const summary = node.container === 'array' ? `[${node.childCount}]` : `{${node.childCount}}`
  return (
    <div data-testid={`json-node-${node.path}`} className="font-mono text-xs">
      <button
        type="button"
        data-testid={`json-node-toggle-${node.path}`}
        aria-expanded={open}
        aria-label={open ? 'Collapse' : 'Expand'}
        className="flex w-full items-start gap-1 text-left hover:bg-accent/40"
        style={{ paddingLeft: padding }}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="mt-0.5 h-3 w-3 shrink-0" />
        )}
        {node.label !== null && (
          <span className="shrink-0 text-muted-foreground">{node.label}:</span>
        )}
        <span className="text-muted-foreground">{summary}</span>
      </button>
      {open && (
        <div>
          {node.children.map((child) => (
            <NodeView key={child.path} node={child} depth={depth + 1} />
          ))}
          {node.hiddenCount > 0 && (
            <div
              data-testid={`json-node-more-${node.path}`}
              className="font-mono text-xs italic text-muted-foreground"
              style={{ paddingLeft: indentFor(depth + 1) }}
            >
              … ({node.hiddenCount} more)
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function leafClass(kind: JsonLeafKind): string {
  switch (kind) {
    case 'string':
      return 'text-emerald-600 dark:text-emerald-400'
    case 'number':
      return 'text-sky-600 dark:text-sky-400'
    case 'boolean':
      return 'text-amber-600 dark:text-amber-400'
    case 'null':
      return 'text-muted-foreground'
  }
}
