import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { TranscriptViewer } from '../TranscriptViewer'
import type { Transcript } from '@/api/autofix-runs'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('TranscriptViewer', () => {
  it('renders text verbatim, preserving newlines via whitespace-pre-wrap', () => {
    const transcript: Transcript = {
      text: 'line one\nline two\nline three',
      truncated: false,
      size_bytes: 30,
    }
    render(<TranscriptViewer transcript={transcript} />)
    const pre = screen.getByTestId('transcript-pre')
    expect(pre.textContent).toBe('line one\nline two\nline three')
    expect(pre.className).toContain('whitespace-pre-wrap')
  })

  it('does not show a truncation banner when truncated is false', () => {
    const transcript: Transcript = { text: 'ok', truncated: false, size_bytes: 2 }
    render(<TranscriptViewer transcript={transcript} />)
    expect(screen.queryByTestId('transcript-truncated-banner')).not.toBeInTheDocument()
  })

  it('shows a truncation banner with a toLocaleString-formatted byte count when truncated', () => {
    const transcript: Transcript = { text: 'partial', truncated: true, size_bytes: 1234567 }
    render(<TranscriptViewer transcript={transcript} />)
    const banner = screen.getByTestId('transcript-truncated-banner')
    expect(banner).toBeInTheDocument()
    expect(banner).toHaveTextContent((1234567).toLocaleString())
  })
})
