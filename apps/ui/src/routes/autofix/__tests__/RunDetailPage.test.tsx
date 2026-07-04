import { afterEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'

import { RunDetailPage } from '../RunDetailPage'
import { useRun, useRunFeedback, useRunTranscript } from '@/api/autofix-runs'
import type { RunDetail, RunFeedback, Transcript } from '@/api/autofix-runs'
import type { ApiError } from '@/api/client'

vi.mock('@/api/autofix-runs', async () => {
  const actual = await vi.importActual<typeof import('@/api/autofix-runs')>('@/api/autofix-runs')
  return {
    ...actual,
    useRun: vi.fn(),
    useRunFeedback: vi.fn(),
    useRunTranscript: vi.fn(),
  }
})

vi.mock('@tanstack/react-router', () => ({
  useParams: () => ({ run_id: 'run-abc' }),
  Link: ({ children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props}>{children}</a>
  ),
}))

function mockQuery<TData>(
  overrides: Record<string, unknown> = {},
): UseQueryResult<TData, ApiError> {
  return {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    ...overrides,
  } as unknown as UseQueryResult<TData, ApiError>
}

function makeRunDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 'run-abc',
    alert_id: null,
    created_at: '2026-07-02T23:58:00Z',
    duration_ms: 5000,
    ended_at: '2026-07-03T00:00:05Z',
    exit_code: 0,
    fixer_user: null,
    host: null,
    initiated_by: 'operator',
    killed_at: null,
    mode: 'dry_run',
    outcome: 'success',
    prompt: null,
    runbook_hash: 'abcdef1234567890',
    runbook_id: 'runbook-1',
    runbook_path: 'runbooks/safe-example',
    started_at: '2026-07-03T00:00:00Z',
    transcript_path: '/var/log/transcripts/run-abc.txt',
    transcript_pruned_at: null,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function setup(overrides: {
  run?: Partial<RunDetail>
  runQueryOverrides?: Record<string, unknown>
  feedback?: RunFeedback[]
  transcript?: Transcript | undefined
  transcriptQueryOverrides?: Record<string, unknown>
}) {
  vi.mocked(useRun).mockReturnValue(
    mockQuery({
      data: makeRunDetail(overrides.run ?? {}),
      ...(overrides.runQueryOverrides ?? {}),
    }),
  )
  vi.mocked(useRunFeedback).mockReturnValue(
    mockQuery({ data: { items: overrides.feedback ?? [] } }),
  )
  vi.mocked(useRunTranscript).mockReturnValue(
    mockQuery({
      data: overrides.transcript,
      ...(overrides.transcriptQueryOverrides ?? {}),
    }),
  )
}

describe('RunDetailPage', () => {
  it('shows a distinct dry-run banner for mode=dry_run', () => {
    setup({ run: { mode: 'dry_run' }, transcript: undefined })
    render(<RunDetailPage />)
    const banner = screen.getByTestId('mode-banner-dry_run')
    expect(banner).toHaveTextContent('Dry-run — plan only')
    expect(banner.className).toContain('yellow')
  })

  it('shows a distinct real-run banner for mode=real', () => {
    setup({ run: { mode: 'real' }, transcript: undefined })
    render(<RunDetailPage />)
    const banner = screen.getByTestId('mode-banner-real')
    expect(banner).toHaveTextContent('Real run')
    expect(banner.className).toContain('blue')
  })

  it('renders metadata card fields: runbook path, hash, exit code, duration, initiator', () => {
    setup({
      run: {
        runbook_path: 'runbooks/safe-example',
        runbook_hash: 'abcdef1234567890',
        exit_code: 3,
        duration_ms: 5000,
        initiated_by: 'operator',
      },
      transcript: undefined,
    })
    render(<RunDetailPage />)
    expect(screen.getByText('runbooks/safe-example')).toBeInTheDocument()
    expect(screen.getByText('abcdef123456')).toBeInTheDocument() // hash sliced to 12 chars
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('5.000s')).toBeInTheDocument()
    expect(screen.getByText('Operator')).toBeInTheDocument()
  })

  it('renders alert initiator with truncated alert id', () => {
    setup({
      run: { initiated_by: 'alert', alert_id: 'abcdefgh12345' },
      transcript: undefined,
    })
    render(<RunDetailPage />)
    expect(screen.getByText('Alert abcdefgh')).toBeInTheDocument()
  })

  it('renders transcript when transcript_path is non-null', () => {
    setup({
      run: { transcript_path: '/var/log/transcripts/run-abc.txt' },
      transcript: { text: 'line one\nline two', truncated: false, size_bytes: 100 },
    })
    render(<RunDetailPage />)
    expect(screen.getByTestId('transcript-pre')).toHaveTextContent('line one line two')
  })

  it('renders "no transcript" text when transcript_path is null', () => {
    setup({
      run: { transcript_path: null },
      transcript: { text: '', truncated: false, size_bytes: 0 },
    })
    render(<RunDetailPage />)
    expect(screen.getByTestId('transcript-absent')).toHaveTextContent(
      'No transcript recorded for this run.',
    )
  })

  it('shows truncation banner when transcript.truncated is true', () => {
    setup({
      run: { transcript_path: '/var/log/transcripts/run-abc.txt' },
      transcript: { text: 'partial', truncated: true, size_bytes: 999999 },
    })
    render(<RunDetailPage />)
    expect(screen.getByTestId('transcript-truncated-banner')).toBeInTheDocument()
  })

  it('renders the feedback list section', () => {
    setup({
      transcript: undefined,
      feedback: [
        {
          id: 'fb-1',
          created_at: '2026-07-03T00:00:10Z',
          kind: 'blocked',
          runbook_run_id: 'run-abc',
          structured_hint: null,
          suggestion_text: 'Needs manual review',
        },
      ],
    })
    render(<RunDetailPage />)
    expect(screen.getByTestId('feedback-item-fb-1')).toBeInTheDocument()
    expect(screen.getByText('Needs manual review')).toBeInTheDocument()
  })

  it('gets distinct badge styling for parse_error feedback kind', () => {
    setup({
      transcript: undefined,
      feedback: [
        {
          id: 'fb-2',
          created_at: '2026-07-03T00:00:10Z',
          kind: 'parse_error',
          runbook_run_id: 'run-abc',
          structured_hint: null,
          suggestion_text: 'Could not parse output',
        },
      ],
    })
    render(<RunDetailPage />)
    expect(screen.getByTestId('feedback-kind-parse_error')).toBeInTheDocument()
  })

  it('renders "Run not found." on error', () => {
    vi.mocked(useRun).mockReturnValue(mockQuery({ isError: true }))
    vi.mocked(useRunFeedback).mockReturnValue(mockQuery({ data: { items: [] } }))
    vi.mocked(useRunTranscript).mockReturnValue(mockQuery({ data: undefined }))
    render(<RunDetailPage />)
    expect(screen.getByRole('alert')).toHaveTextContent('Run not found.')
  })

  it('has no delete affordance', () => {
    setup({ transcript: undefined })
    const { container } = render(<RunDetailPage />)
    expect(screen.queryAllByText(/^delete$/i).length).toBe(0)
    expect(container.querySelectorAll('[data-testid*="delete" i]').length).toBe(0)
  })
})
