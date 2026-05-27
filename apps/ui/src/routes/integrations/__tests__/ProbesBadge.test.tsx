import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProbesBadge } from '@/routes/integrations/ProbesBadge'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/docker', () => ({
  useProbesSummary: vi.fn(),
  dockerProbeQueryKeys: {
    summary: ['integrations', 'docker', 'probes-summary'] as const,
  },
}))

import { useProbesSummary } from '@/api/docker'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeProbeSummary(
  overrides: Record<
    string,
    {
      active: number
      failing: number
      config_errors?: string[] | null
      source_breakdown?: Record<string, number>
    }
  > = {},
) {
  return {
    homeassistant: {
      active: 2,
      failing: 0,
      config_errors: null,
      source_breakdown: { label: 2 },
    },
    pihole: {
      active: 1,
      failing: 1,
      config_errors: null,
      source_breakdown: { auto_default: 1 },
    },
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderBadge(containerName: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProbesBadge containerName={containerName} />
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ProbesBadge', () => {
  beforeEach(() => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary(),
    } as never)
  })

  it('renders nothing when isPending is true', () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: true,
      isFetching: false,
      isLoading: true,
      error: null,
      data: undefined,
    } as never)
    const { container } = renderBadge('homeassistant')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when no entry exists', () => {
    const { container } = renderBadge('nonexistent')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when active === 0 and no config errors', () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary({
        inactive: {
          active: 0,
          failing: 0,
          config_errors: null,
          source_breakdown: {},
        },
      }),
    } as never)
    const { container } = renderBadge('inactive')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders "X active" badge when active > 0 and failing === 0', () => {
    renderBadge('homeassistant')
    expect(screen.getByText('2 active')).toBeInTheDocument()
  })

  it('renders "X failing" badge when failing > 0', () => {
    renderBadge('pihole')
    expect(screen.getByText('1 failing')).toBeInTheDocument()
  })

  it('does not render active count when failing > 0', () => {
    renderBadge('pihole')
    expect(screen.queryByText('1 active')).not.toBeInTheDocument()
  })

  it('renders "Config errors" badge when config_errors is non-empty', () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary({
        homeassistant: {
          active: 2,
          failing: 0,
          config_errors: ['Invalid YAML syntax', 'Unknown probe kind'],
          source_breakdown: { label: 1, file_override: 1 },
        },
      }),
    } as never)
    renderBadge('homeassistant')
    expect(screen.getByText('Config errors')).toBeInTheDocument()
  })

  it('Config errors badge takes priority over active count', () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary({
        homeassistant: {
          active: 5,
          failing: 2,
          config_errors: ['Error 1'],
          source_breakdown: { label: 5 },
        },
      }),
    } as never)
    renderBadge('homeassistant')
    expect(screen.getByText('Config errors')).toBeInTheDocument()
    expect(screen.queryByText(/active/)).not.toBeInTheDocument()
    expect(screen.queryByText(/failing/)).not.toBeInTheDocument()
  })

  it('renders config error aria-label with all errors joined by semicolon', () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary({
        homeassistant: {
          active: 2,
          failing: 0,
          config_errors: ['Error A', 'Error B'],
          source_breakdown: { label: 2 },
        },
      }),
    } as never)
    renderBadge('homeassistant')
    expect(
      screen.getByLabelText('Config error for homeassistant: Error A; Error B'),
    ).toBeInTheDocument()
  })
})
