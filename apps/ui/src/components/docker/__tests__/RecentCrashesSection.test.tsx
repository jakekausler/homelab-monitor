// Project test conventions:
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom
// - Mocking: vi.mock() factory at top, vi.clearAllMocks() in afterEach
// - Async: none needed for these sync-render tests
// - Render harness: QueryClientProvider + TooltipProvider

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// ---------------------------------------------------------------------------
// Mocks — must be hoisted above component import
// ---------------------------------------------------------------------------

const mockUseContainerCrashes = vi.fn()
const mockUseContainerCrashDetail = vi.fn()

vi.mock('@/api/docker', () => ({
  useContainerCrashes: (): unknown => mockUseContainerCrashes(),
  useContainerCrashDetail: (...a: unknown[]): unknown => mockUseContainerCrashDetail(...a),
}))

vi.mock('@/components/logs/OpenInExplorerButton', () => ({
  OpenInExplorerButton: () => <div data-testid="open-explorer" />,
}))

import { TooltipProvider } from '@/components/ui/tooltip'
import { RecentCrashesSection } from '../RecentCrashesSection'

// ---------------------------------------------------------------------------
// Types (no `any`)
// ---------------------------------------------------------------------------

type CrashSummary = {
  crash_id: string
  exit_code: number
  finished_at: string
  image_name: string | null
  compose_project: string | null
  compose_service: string | null
  line_count: number
  truncated: boolean
  degraded: boolean
  created_at: string
}

type CrashDetailData = {
  crash_id: string
  container_name: string
  exit_code: number
  finished_at: string
  image_name: string | null
  compose_project: string | null
  compose_service: string | null
  line_count: number
  truncated: boolean
  degraded: boolean
  created_at: string
  window_start: string
  window_end: string
  lines: Array<{
    timestamp: string
    message: string
    stream: string
    severity: string
    host: string | null
    service: string | null
    fields: Record<string, string>
  }>
}

function req<T>(v: T | undefined): T {
  if (v === undefined) throw new Error('Expected value but got undefined')
  return v
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function wrap(ui: React.ReactNode): ReturnType<typeof render> {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  )
}

const CRASH_SUMMARY: CrashSummary = {
  crash_id: 'x',
  exit_code: 1,
  finished_at: '2026-06-07T00:00:00Z',
  image_name: null,
  compose_project: null,
  compose_service: null,
  line_count: 3,
  truncated: false,
  degraded: false,
  created_at: '2026-06-07T00:00:01Z',
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RecentCrashesSection', () => {
  it('renders crash rows when crashes present', () => {
    mockUseContainerCrashes.mockReturnValue({
      data: { container_name: 'c', crashes: [CRASH_SUMMARY] },
      isPending: false,
      isError: false,
      error: undefined,
    })
    mockUseContainerCrashDetail.mockReturnValue({
      isPending: true,
      data: undefined,
      isError: false,
      error: undefined,
    })

    wrap(<RecentCrashesSection containerName="c" />)

    expect(screen.getByTestId('recent-crashes-section')).toBeInTheDocument()
    expect(req(screen.getAllByTestId('crash-row')[0])).toBeInTheDocument()
    expect(screen.getAllByText('exit 1').length).toBeGreaterThanOrEqual(1)
  })

  it('renders empty state when crashes list is empty', () => {
    mockUseContainerCrashes.mockReturnValue({
      data: { container_name: 'c', crashes: [] },
      isPending: false,
      isError: false,
      error: undefined,
    })

    wrap(<RecentCrashesSection containerName="c" />)

    expect(screen.getByTestId('recent-crashes-empty')).toBeInTheDocument()
  })

  it('shows loading indicator while pending', () => {
    mockUseContainerCrashes.mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      error: undefined,
    })

    wrap(<RecentCrashesSection containerName="c" />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows error display when query errors', () => {
    const err = { status: 500, message: 'internal error', details: {} }
    mockUseContainerCrashes.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: err,
    })

    wrap(<RecentCrashesSection containerName="c" />)

    expect(screen.getByTestId('recent-crashes-section')).toBeInTheDocument()
    // ErrorDisplay renders the error — section still present
    expect(screen.queryByTestId('recent-crashes-empty')).toBeNull()
  })

  it('expands crash and shows detail logs on click', () => {
    const detail: CrashDetailData = {
      crash_id: 'x',
      container_name: 'c',
      exit_code: 1,
      finished_at: '2026-06-07T00:00:00Z',
      image_name: null,
      compose_project: null,
      compose_service: null,
      line_count: 1,
      truncated: false,
      degraded: false,
      created_at: '2026-06-07T00:00:01Z',
      window_start: '2026-06-07T00:00:00Z',
      window_end: '2026-06-07T00:01:00Z',
      lines: [
        {
          timestamp: '2026-06-07T00:00:00Z',
          message: 'boom',
          stream: 's',
          severity: 'error',
          host: null,
          service: null,
          fields: {},
        },
      ],
    }

    mockUseContainerCrashes.mockReturnValue({
      data: { container_name: 'c', crashes: [CRASH_SUMMARY] },
      isPending: false,
      isError: false,
      error: undefined,
    })

    mockUseContainerCrashDetail.mockReturnValue({
      isPending: false,
      isError: false,
      error: undefined,
      data: detail,
    })

    wrap(<RecentCrashesSection containerName="c" />)

    fireEvent.click(screen.getByTestId('crash-expand-x'))

    expect(screen.getByTestId('crash-logviewer')).toBeInTheDocument()
    expect(screen.getAllByText('boom').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByTestId('open-explorer')).toBeInTheDocument()
  })
})
