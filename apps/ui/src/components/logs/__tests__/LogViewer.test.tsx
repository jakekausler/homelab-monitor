import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import { LogViewer } from '@/components/logs/LogViewer'
import type { LogLine, UseLogsResult } from '@/components/logs/types'

afterEach(cleanup)

function line(message: string, severity: string | null = null): LogLine {
  return {
    timestamp: '2026-05-21T14:30:00Z',
    message,
    stream: 'stdout',
    severity,
    host: null,
    service: null,
    fields: {},
  }
}

function makeUseLogs(r: Partial<UseLogsResult>): () => UseLogsResult {
  return () => ({
    lines: undefined,
    isLoading: false,
    isError: false,
    error: undefined,
    ...r,
  })
}

describe('LogViewer', () => {
  it('renders available lines in local time by default', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [line('INFO a'), line('INFO b')],
        })}
      />,
    )

    const body = screen.getByTestId('logs-body')
    expect(body.textContent).toContain('INFO a')
    expect(body.textContent).toContain('INFO b')
    // Default is local (America/New_York, EDT in May).
    expect(body.textContent).toContain('2026-05-21 10:30:00 EDT')
  })

  it('renders timestamps in UTC when timezone="utc"', () => {
    render(
      <LogViewer
        timezone="utc"
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [line('INFO a')],
        })}
      />,
    )

    const body = screen.getByTestId('logs-body')
    expect(body.textContent).toContain('2026-05-21 14:30:00 UTC')
  })

  it('timestamp span carries the other-zone tooltip', () => {
    render(
      <LogViewer
        timezone="local"
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [line('INFO a')],
        })}
      />,
    )

    expect(screen.getByTitle('2026-05-21 14:30:00 UTC')).toBeInTheDocument()
  })

  it('renders available with empty lines', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [],
        })}
      />,
    )

    const body = screen.getByTestId('logs-body')
    expect(body).toBeInTheDocument()
    expect(body.textContent).toBe('')
  })

  it('renders no_lines with default copy', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'no_lines',
        })}
      />,
    )

    expect(screen.getByTestId('no-lines')).toBeInTheDocument()
    expect(screen.getByText('No log lines.')).toBeInTheDocument()
  })

  it('renders no_lines with custom copy', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'no_lines',
        })}
        emptyStateCopy="Nothing here"
      />,
    )

    expect(screen.getByTestId('no-lines')).toBeInTheDocument()
    expect(screen.getByText('Nothing here')).toBeInTheDocument()
  })

  it('renders unavailable with default copy', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'unavailable',
          isError: true,
        })}
      />,
    )

    const banner = screen.getByTestId('unavailable-banner')
    expect(banner).toBeInTheDocument()
    expect(banner).toHaveAttribute('role', 'status')
    expect(banner).toHaveTextContent(
      'Logs temporarily unavailable. The Refresh button still works.',
    )
  })

  it('renders unavailable with custom copy', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'unavailable',
          isError: true,
        })}
        unavailableCopy="Down hard"
      />,
    )

    expect(screen.getByTestId('unavailable-banner')).toHaveTextContent('Down hard')
  })

  it('renders unknown state', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'unknown',
          isError: true,
        })}
      />,
    )

    expect(screen.getByTestId('logs-unknown')).toBeInTheDocument()
    expect(screen.getByText('Logs source not found.')).toBeInTheDocument()
  })

  it('renders unknown with custom copy via unavailableCopy', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'unknown',
          isError: true,
        })}
        unavailableCopy="Custom unknown"
      />,
    )

    expect(screen.getByText('Custom unknown')).toBeInTheDocument()
  })

  it('renders expired state', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'expired',
        })}
      />,
    )

    expect(screen.getByTestId('expired-notice')).toBeInTheDocument()
    expect(screen.queryByTestId('logs-body')).not.toBeInTheDocument()
  })

  it('renders running banner', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'running',
          lines: [line('x')],
        })}
      />,
    )

    expect(screen.getByTestId('running-banner')).toBeInTheDocument()
    expect(screen.getByTestId('logs-body')).toBeInTheDocument()
  })

  it('renders truncated banner', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          truncated: true,
          lines: [line('x')],
        })}
      />,
    )

    const banner = screen.getByTestId('truncated-banner')
    expect(banner).toBeInTheDocument()
    expect(banner).toHaveTextContent('Showing first 1 lines')
  })

  it('does not render truncated banner when false', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          truncated: false,
          lines: [line('x')],
        })}
      />,
    )

    expect(screen.queryByTestId('truncated-banner')).not.toBeInTheDocument()
  })

  it('renders error via ErrorDisplay', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          isError: true,
          error: new ApiError({
            status: 500,
            code: 'internal_error',
            message: 'Boom',
            retryAfterSeconds: null,
            details: null,
          }),
        })}
      />,
    )

    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Boom/)).toBeInTheDocument()
  })

  it('renders loading state', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          isLoading: true,
        })}
      />,
    )

    expect(screen.getByText('Loading logs…')).toBeInTheDocument()
  })

  it('renders headerSlot', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [],
        })}
        headerSlot={<div data-testid="hdr">H</div>}
      />,
    )

    expect(screen.getByTestId('hdr')).toBeInTheDocument()
  })
})
