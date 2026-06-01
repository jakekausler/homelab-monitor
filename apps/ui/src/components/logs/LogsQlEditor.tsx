// STAGE-004-011 — shared, reusable LogsQL editor.
//
// Shell component with the same value/onChange/onSubmit contract as the plain
// search input, so the Body renders exactly one of them based on advancedMode.
//
// - Narrow viewports → a plain <textarea> (tap-friendly, no CodeMirror loaded).
// - Wide viewports → lazily-loaded CodeMirror 6 editor (LogsQlEditorImpl), with
//   the SAME plain <textarea> as the Suspense fallback during chunk load.
//
// The eager bundle must NOT statically import @codemirror/* — only the
// React.lazy() boundary pulls LogsQlEditorImpl (and CodeMirror) into a chunk.

import { Suspense, lazy } from 'react'

import { useMediaQuery } from '@/lib/useMediaQuery'

export interface LogsQlEditorProps {
  value: string
  onChange: (next: string) => void
  onSubmit: () => void
  placeholder?: string
  ariaLabel: string
  className?: string
}

const LazyLogsQlEditorImpl = lazy(() => import('./LogsQlEditorImpl'))

const TEXTAREA_CLASS =
  'flex min-h-9 w-full max-w-md rounded-md border border-input bg-background px-3 py-1 font-mono text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring'

/**
 * Plain controlled textarea used both for the narrow-viewport editor and as the
 * Suspense fallback while the CodeMirror chunk loads. Enter submits (Shift+Enter
 * inserts a newline), matching the CodeMirror keymap.
 */
function PlainTextareaEditor({
  value,
  onChange,
  onSubmit,
  placeholder,
  ariaLabel,
  className,
}: LogsQlEditorProps) {
  return (
    <textarea
      data-testid="logsql-editor-textarea"
      aria-label={ariaLabel}
      className={className ?? TEXTAREA_CLASS}
      placeholder={placeholder}
      rows={1}
      value={value}
      onChange={(e) => {
        onChange(e.target.value)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault()
          onSubmit()
        }
      }}
    />
  )
}

export function LogsQlEditor(props: LogsQlEditorProps) {
  // Desktop breakpoint matches the rest of the app's md: usage (768px).
  const isWide = useMediaQuery('(min-width: 768px)')

  if (!isWide) {
    return <PlainTextareaEditor {...props} />
  }

  return (
    <Suspense fallback={<PlainTextareaEditor {...props} />}>
      <LazyLogsQlEditorImpl {...props} />
    </Suspense>
  )
}
