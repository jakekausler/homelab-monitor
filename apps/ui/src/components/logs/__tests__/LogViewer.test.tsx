import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'

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

  it('STAGE-004-016: without fieldInspectorEnabled, rows are not clickable', () => {
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [line('INFO a'), line('INFO b')],
        })}
        fieldInspectorEnabled={false}
      />,
    )

    // No button roles on log rows when inspector disabled
    const buttons = screen.queryAllByRole('button')
    // Should only have load-older button if present, but with no pagination in this test: empty or only other buttons
    const logRowButtons = buttons.filter((b) => {
      const aria = b.getAttribute('aria-label')
      return aria?.includes('log') || aria?.includes('row')
    })
    expect(logRowButtons).toHaveLength(0)
  })

  it('STAGE-004-016: with fieldInspectorEnabled, clicking a row calls onInspectLine with the line', async () => {
    const onInspectLineMock = vi.fn()
    const testLine = line('Test message')
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [testLine],
        })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
      />,
    )

    const body = screen.getByTestId('logs-body')
    const firstRow = body.querySelector('[role="button"]')
    expect(firstRow).toBeInTheDocument()

    if (firstRow instanceof Element) {
      await userEvent.click(firstRow)

      expect(onInspectLineMock).toHaveBeenCalledTimes(1)
      expect(onInspectLineMock).toHaveBeenCalledWith(testLine)
    }
  })

  it('STAGE-004-016: clicking the selected row again deselects and calls onInspectLine(null)', async () => {
    const onInspectLineMock = vi.fn()
    const testLine = line('Test message')
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [testLine],
        })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
      />,
    )

    const body = screen.getByTestId('logs-body')
    const firstRow = body.querySelector('[role="button"]')

    if (firstRow instanceof Element) {
      await userEvent.click(firstRow)
      expect(onInspectLineMock).toHaveBeenCalledWith(testLine)

      await userEvent.click(firstRow)
      expect(onInspectLineMock).toHaveBeenLastCalledWith(null)
    }
  })

  it('STAGE-004-016: clicking different rows swaps selection', async () => {
    const onInspectLineMock = vi.fn()
    const line1 = line('Message 1')
    const line2 = line('Message 2')
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [line1, line2],
        })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
      />,
    )

    const body = screen.getByTestId('logs-body')
    const rows = body.querySelectorAll('[role="button"]')
    expect(rows).toHaveLength(2)

    const row0 = rows[0]
    if (row0) {
      await userEvent.click(row0)
      expect(onInspectLineMock).toHaveBeenLastCalledWith(line1)
    }

    const row1 = rows[1]
    if (row1) {
      await userEvent.click(row1)
      expect(onInspectLineMock).toHaveBeenLastCalledWith(line2)
    }
  })

  it('STAGE-004-016: selected row has data-testid="log-row-selected"', async () => {
    const testLine = line('Test message')
    render(
      <LogViewer
        useLogs={makeUseLogs({
          logStatus: 'available',
          lines: [testLine],
        })}
        fieldInspectorEnabled
      />,
    )

    const body = screen.getByTestId('logs-body')
    let selectedRow = screen.queryByTestId('log-row-selected')
    expect(selectedRow).not.toBeInTheDocument()

    const firstRow = body.querySelector('[role="button"]')
    await userEvent.click(firstRow!)

    selectedRow = screen.getByTestId('log-row-selected')
    expect(selectedRow).toBeInTheDocument()
  })

  it('STAGE-004-016 fix: controlled selectedKey — highlight follows parent, not internal state', () => {
    // When selectedKey prop is provided, LogViewer defers to it for highlight.
    const onInspectLineMock = vi.fn()
    const testLine = line('Controlled row')
    const { rerender } = render(
      <LogViewer
        useLogs={makeUseLogs({ logStatus: 'available', lines: [testLine] })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
        selectedKey={null}
      />,
    )
    // Initially null → no selected row.
    expect(screen.queryByTestId('log-row-selected')).not.toBeInTheDocument()

    // Simulate parent passing the key after a click.
    const body = screen.getByTestId('logs-body')
    const row = body.querySelector('[role="button"]')
    expect(row).toBeInTheDocument()
    // Derive key from data-testid after click: after rerender with key it should show selected.
    // (We inject the key directly since it's timestamp-index based.)
    const key = '2026-05-21T14:30:00Z-0'
    rerender(
      <LogViewer
        useLogs={makeUseLogs({ logStatus: 'available', lines: [testLine] })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
        selectedKey={key}
      />,
    )
    expect(screen.getByTestId('log-row-selected')).toBeInTheDocument()

    // Parent clears key → highlight gone.
    rerender(
      <LogViewer
        useLogs={makeUseLogs({ logStatus: 'available', lines: [testLine] })}
        fieldInspectorEnabled
        onInspectLine={onInspectLineMock}
        selectedKey={null}
      />,
    )
    expect(screen.queryByTestId('log-row-selected')).not.toBeInTheDocument()
  })

  it('STAGE-004-016 fix: after close (selectedKey=null), single click reopens (onLineSelected called)', async () => {
    // Regression test for two-click-reopen bug.
    // When parent controls selectedKey and resets it to null on close,
    // the next click on the same row should fire onLineSelected(line, key) — not null.
    const onLineSelectedMock = vi.fn()
    const testLine = line('Reopen test')
    render(
      <LogViewer
        useLogs={makeUseLogs({ logStatus: 'available', lines: [testLine] })}
        fieldInspectorEnabled
        onLineSelected={onLineSelectedMock}
        selectedKey={null}
      />,
    )
    const body = screen.getByTestId('logs-body')
    const row = body.querySelector('[role="button"]')
    expect(row).toBeInTheDocument()

    // First click → open.
    await userEvent.click(row!)
    expect(onLineSelectedMock).toHaveBeenCalledTimes(1)
    expect(onLineSelectedMock.mock.calls[0]?.[0]).toEqual(testLine)

    // Parent reset selectedKey to null (simulated by prop; no rerender needed
    // since component is controlled and selectedKey is still null here).
    // Second click → should open again (not close), because selectedKey===null !== key.
    await userEvent.click(row!)
    expect(onLineSelectedMock).toHaveBeenCalledTimes(2)
    expect(onLineSelectedMock.mock.calls[1]?.[0]).toEqual(testLine)
  })
})
