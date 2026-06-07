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

const mockUseContainerHealthcheckIncidents = vi.fn()
const mockUseContainerHealthcheckIncidentDetail = vi.fn()

vi.mock('@/api/docker', async (importActual) => ({
  ...(await importActual<typeof import('@/api/docker')>()),
  useContainerHealthcheckIncidents: (): unknown => mockUseContainerHealthcheckIncidents(),
  useContainerHealthcheckIncidentDetail: (...a: unknown[]): unknown =>
    mockUseContainerHealthcheckIncidentDetail(...a),
}))

vi.mock('@/components/logs/OpenInExplorerButton', () => ({
  OpenInExplorerButton: () => <div data-testid="open-explorer" />,
}))

import { TooltipProvider } from '@/components/ui/tooltip'
import { RecentHealthcheckIncidentsSection } from '../RecentHealthcheckIncidentsSection'

// ---------------------------------------------------------------------------
// Types (no `any`)
// ---------------------------------------------------------------------------

type IncidentSummary = {
  incident_id: string
  previous_healthcheck: string | null
  new_state: string
  healthcheck_changed_at: string
  image_name: string | null
  compose_project: string | null
  compose_service: string | null
  line_count: number
  truncated: boolean
  degraded: boolean
  created_at: string
}

type IncidentDetailData = {
  incident_id: string
  container_name: string
  previous_healthcheck: string | null
  new_state: string
  healthcheck_changed_at: string
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

const INCIDENT_SUMMARY: IncidentSummary = {
  incident_id: 'hc-1',
  previous_healthcheck: 'healthy',
  new_state: 'unhealthy',
  healthcheck_changed_at: '2026-06-07T00:00:00Z',
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

describe('RecentHealthcheckIncidentsSection', () => {
  it('renders incident rows when incidents present', () => {
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: { container_name: 'c', incidents: [INCIDENT_SUMMARY] },
      isPending: false,
      isError: false,
      error: undefined,
    })
    mockUseContainerHealthcheckIncidentDetail.mockReturnValue({
      isPending: true,
      data: undefined,
      isError: false,
      error: undefined,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    expect(screen.getByTestId('recent-healthcheck-section')).toBeInTheDocument()
    expect(req(screen.getAllByTestId('healthcheck-row')[0])).toBeInTheDocument()
    // Transition label: "healthy → unhealthy"
    expect(screen.getAllByText('healthy → unhealthy').length).toBeGreaterThanOrEqual(1)
  })

  it('renders transition label with null previous as "→ unhealthy"', () => {
    const incidentNoPrev: IncidentSummary = { ...INCIDENT_SUMMARY, previous_healthcheck: null }
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: { container_name: 'c', incidents: [incidentNoPrev] },
      isPending: false,
      isError: false,
      error: undefined,
    })
    mockUseContainerHealthcheckIncidentDetail.mockReturnValue({
      isPending: true,
      data: undefined,
      isError: false,
      error: undefined,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    expect(screen.getAllByText('→ unhealthy').length).toBeGreaterThanOrEqual(1)
  })

  it('renders empty state when incidents list is empty', () => {
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: { container_name: 'c', incidents: [] },
      isPending: false,
      isError: false,
      error: undefined,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    expect(screen.getByTestId('recent-healthcheck-empty')).toBeInTheDocument()
  })

  it('shows loading indicator while pending', () => {
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      error: undefined,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows error display when query errors', () => {
    const err = { status: 500, message: 'internal error', details: {} }
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: err,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    expect(screen.getByTestId('recent-healthcheck-section')).toBeInTheDocument()
    // ErrorDisplay renders the error — empty state is NOT shown
    expect(screen.queryByTestId('recent-healthcheck-empty')).toBeNull()
  })

  it('expands incident and shows detail logs on click', () => {
    const detail: IncidentDetailData = {
      incident_id: 'hc-1',
      container_name: 'c',
      previous_healthcheck: 'healthy',
      new_state: 'unhealthy',
      healthcheck_changed_at: '2026-06-07T00:00:00Z',
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
          message: 'healthcheck failed',
          stream: 's',
          severity: 'error',
          host: null,
          service: null,
          fields: {},
        },
      ],
    }

    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: { container_name: 'c', incidents: [INCIDENT_SUMMARY] },
      isPending: false,
      isError: false,
      error: undefined,
    })

    mockUseContainerHealthcheckIncidentDetail.mockReturnValue({
      isPending: false,
      isError: false,
      error: undefined,
      data: detail,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    fireEvent.click(screen.getByTestId('healthcheck-expand-hc-1'))

    expect(screen.getByTestId('healthcheck-logviewer')).toBeInTheDocument()
    expect(screen.getAllByText('healthcheck failed').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByTestId('open-explorer')).toBeInTheDocument()
  })

  it('detail hook is called with enabled=true when expanded', () => {
    mockUseContainerHealthcheckIncidents.mockReturnValue({
      data: { container_name: 'c', incidents: [INCIDENT_SUMMARY] },
      isPending: false,
      isError: false,
      error: undefined,
    })
    mockUseContainerHealthcheckIncidentDetail.mockReturnValue({
      isPending: true,
      data: undefined,
      isError: false,
      error: undefined,
    })

    wrap(<RecentHealthcheckIncidentsSection containerName="c" />)

    fireEvent.click(screen.getByTestId('healthcheck-expand-hc-1'))

    // After expand, the detail panel is rendered and the hook is called with enabled=true
    expect(mockUseContainerHealthcheckIncidentDetail).toHaveBeenCalledWith('c', 'hc-1', true)
  })
})
