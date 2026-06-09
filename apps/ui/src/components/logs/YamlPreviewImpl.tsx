import { useEffect, useRef } from 'react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { yaml as yamlLang } from '@codemirror/lang-yaml'

interface YamlPreviewImplProps {
  value: string
  ariaLabel: string
  className?: string
}

/**
 * Read-only CodeMirror 6 YAML viewer. Default-exported so React.lazy() in
 * YamlPreview can code-split the @codemirror/* imports out of the eager bundle
 * (mirrors LogsQlEditorImpl, STAGE-004-011 / 043).
 */
export default function YamlPreviewImpl({ value, ariaLabel, className }: YamlPreviewImplProps) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const viewRef = useRef<EditorView | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (host === null) return
    const state = EditorState.create({
      doc: value,
      extensions: [
        yamlLang(),
        EditorView.lineWrapping,
        EditorView.editable.of(false),
        EditorState.readOnly.of(true),
        EditorView.theme({
          '&': {
            width: '100%',
            maxHeight: '60vh',
            fontSize: '0.8125rem',
            border: '1px solid hsl(var(--input))',
            borderRadius: '0.375rem',
            backgroundColor: 'hsl(var(--muted))',
          },
          '.cm-scroller': { overflow: 'auto', maxHeight: '60vh' },
          '.cm-content': {
            padding: '0.5rem 0.75rem',
            fontFamily: 'ui-monospace, monospace',
          },
        }),
        EditorView.contentAttributes.of({ 'aria-label': ariaLabel }),
      ],
    })
    const view = new EditorView({ state, parent: host })
    viewRef.current = view
    return () => {
      view.destroy()
      viewRef.current = null
    }
    // Mount once; value sync handled in the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- create view once on mount
  }, [])

  // Sync external value changes into the read-only doc.
  useEffect(() => {
    const view = viewRef.current
    if (view === null) return
    const current = view.state.doc.toString()
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } })
    }
  }, [value])

  return <div ref={hostRef} data-testid="yaml-preview-cm" className={className} />
}
