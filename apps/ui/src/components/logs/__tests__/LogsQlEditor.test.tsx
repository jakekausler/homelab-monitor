import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Force the narrow-viewport textarea path: useMediaQuery('(max-width: 767px)')
// returns true → the shell renders the plain <textarea> directly (no CodeMirror,
// which is non-functional in jsdom). The CM6 path is validated in manual
// Refinement. This exercises the SHARED value/onChange/onSubmit contract.
vi.mock('@/lib/useMediaQuery', () => ({
  useMediaQuery: vi.fn(() => true),
}))

import { LogsQlEditor } from '@/components/logs/LogsQlEditor'
import { sanitizeSingleLineInsert } from '@/components/logs/logsQlEditorUtils'

afterEach(cleanup)

describe('LogsQlEditor (textarea fallback path)', () => {
  function renderEditor(
    overrides: {
      value?: string
      onChange?: (next: string) => void
      onSubmit?: () => void
      placeholder?: string
      ariaLabel?: string
    } = {},
  ) {
    const onChange = overrides.onChange ?? vi.fn()
    const onSubmit = overrides.onSubmit ?? vi.fn()
    render(
      <LogsQlEditor
        value={overrides.value ?? ''}
        onChange={onChange}
        onSubmit={onSubmit}
        placeholder={overrides.placeholder ?? 'Enter LogsQL…'}
        ariaLabel={overrides.ariaLabel ?? 'LogsQL query'}
      />,
    )
    return { onChange, onSubmit }
  }

  it('renders the plain textarea with aria-label and placeholder', () => {
    renderEditor()
    const ta = screen.getByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(ta).toBeInTheDocument()
    expect(ta).toHaveAttribute('aria-label', 'LogsQL query')
    expect(ta).toHaveAttribute('placeholder', 'Enter LogsQL…')
  })

  it('calls onChange with the new value on input', () => {
    const { onChange } = renderEditor()
    const ta = screen.getByTestId('logsql-editor-textarea')
    fireEvent.change(ta, { target: { value: 'service:foo' } })
    expect(onChange).toHaveBeenCalledWith('service:foo')
  })

  it('submits on plain Enter (no shift)', () => {
    const { onSubmit } = renderEditor({ value: 'service:foo' })
    const ta = screen.getByTestId('logsql-editor-textarea')
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: false })
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('calls preventDefault on plain Enter so the textarea inserts no newline', () => {
    renderEditor({ value: 'service:foo' })
    const ta = screen.getByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    const prevented = !fireEvent.keyDown(ta, { key: 'Enter', shiftKey: false })
    expect(prevented).toBe(true)
  })

  it('does NOT submit on Shift+Enter', () => {
    const { onSubmit } = renderEditor({ value: 'service:foo' })
    const ta = screen.getByTestId('logsql-editor-textarea')
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('reflects the controlled value prop', () => {
    renderEditor({ value: 'host:nas AND severity:error' })
    const ta = screen.getByTestId<HTMLTextAreaElement>('logsql-editor-textarea')
    expect(ta.value).toBe('host:nas AND severity:error')
  })
})

describe('sanitizeSingleLineInsert', () => {
  it('drops a bare newline (Enter inserts nothing)', () => {
    expect(sanitizeSingleLineInsert('\n')).toBe('')
  })
  it('drops CRLF and multiple newlines', () => {
    expect(sanitizeSingleLineInsert('\r\n')).toBe('')
    expect(sanitizeSingleLineInsert('\n\n')).toBe('')
  })
  it('collapses newlines among content (multi-line paste) to a space', () => {
    expect(sanitizeSingleLineInsert('foo\nbar')).toBe('foo bar')
  })
  it('leaves newline-free text unchanged', () => {
    expect(sanitizeSingleLineInsert('foo bar')).toBe('foo bar')
  })
  it('preserves trailing content with a collapsed space', () => {
    expect(sanitizeSingleLineInsert('foo\n')).toBe('foo ')
  })
})
