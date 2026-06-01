import { useEffect, useRef } from 'react'
import { defaultKeymap } from '@codemirror/commands'
import { EditorState } from '@codemirror/state'
import { EditorView, keymap, placeholder as cmPlaceholder } from '@codemirror/view'

import { logsQlExtensions } from './logsQlLanguage'

interface LogsQlEditorImplProps {
  value: string
  onChange: (next: string) => void
  onSubmit: () => void
  placeholder?: string
  ariaLabel: string
  className?: string
}

/**
 * Hand-wired CodeMirror 6 editor (no @uiw wrapper). Default-exported so
 * React.lazy() in LogsQlEditor can code-split it. This is the ONLY module that
 * statically imports @codemirror/view/state/commands; keeping those imports
 * behind the lazy boundary keeps them out of the eager bundle. STAGE-004-011.
 */
export default function LogsQlEditorImpl({
  value,
  onChange,
  onSubmit,
  placeholder,
  ariaLabel,
  className,
}: LogsQlEditorImplProps) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const viewRef = useRef<EditorView | null>(null)
  // Keep the latest callbacks in refs so the EditorState (created once) always
  // calls the current onChange/onSubmit without re-creating the view. Assign in
  // an effect (not during render) to satisfy react-hooks/refs.
  const onChangeRef = useRef(onChange)
  const onSubmitRef = useRef(onSubmit)
  useEffect(() => {
    onChangeRef.current = onChange
    onSubmitRef.current = onSubmit
  })

  useEffect(() => {
    const host = hostRef.current
    if (host === null) return

    const submitKeymap = keymap.of([
      {
        key: 'Enter',
        run: () => {
          onSubmitRef.current()
          return true // handled — prevents inserting a newline
        },
      },
      {
        key: 'Shift-Enter',
        run: (view): boolean => {
          view.dispatch(view.state.replaceSelection('\n'))
          return true
        },
      },
    ])

    const state = EditorState.create({
      doc: value,
      extensions: [
        submitKeymap, // BEFORE defaultKeymap so plain Enter submits, not newline
        keymap.of(defaultKeymap),
        EditorView.lineWrapping,
        ...(placeholder !== undefined ? [cmPlaceholder(placeholder)] : []),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            onChangeRef.current(update.state.doc.toString())
          }
        }),
        // The CM .cm-editor IS the bordered box (the host div is just a width
        // wrapper). .cm-content fills the full width + a min-height so clicks
        // anywhere in the box position the caret; caretColor makes it visible.
        EditorView.theme({
          '&': {
            width: '100%',
            minHeight: '2.25rem',
            fontSize: '0.875rem',
            border: '1px solid hsl(var(--input))',
            borderRadius: '0.375rem',
            backgroundColor: 'hsl(var(--background))',
            boxShadow: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
          },
          '.cm-scroller': {
            minHeight: '2.25rem',
            overflowX: 'auto',
          },
          '.cm-content': {
            minHeight: '2.25rem',
            padding: '0.375rem 0.75rem',
            fontFamily: 'ui-monospace, monospace',
            caretColor: 'hsl(var(--foreground))',
          },
          '&.cm-focused': {
            outline: 'none',
            boxShadow: '0 0 0 1px hsl(var(--ring))',
          },
          '&.cm-focused .cm-cursor': {
            borderLeftColor: 'hsl(var(--foreground))',
          },
        }),
        EditorView.contentAttributes.of({ 'aria-label': ariaLabel }),
        ...logsQlExtensions(),
      ],
    })

    const view = new EditorView({ state, parent: host })
    viewRef.current = view

    return () => {
      view.destroy()
      viewRef.current = null
    }
    // Mount once. Value sync + callback freshness are handled separately
    // (callbacks via refs, value via the effect below).
    // eslint-disable-next-line react-hooks/exhaustive-deps -- create the view exactly once on mount
  }, [])

  // Sync external value changes into the view WITHOUT clobbering user edits:
  // only dispatch when the incoming value differs from the current doc.
  useEffect(() => {
    const view = viewRef.current
    if (view === null) return
    const current = view.state.doc.toString()
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } })
    }
  }, [value])

  return <div ref={hostRef} data-testid="logsql-editor-cm" className={className} />
}
