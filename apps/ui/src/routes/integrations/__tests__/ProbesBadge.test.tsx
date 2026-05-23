import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
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
// Router helper
// ---------------------------------------------------------------------------

function renderWithRouter(containerName: string) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const integrationsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/integrations',
    component: () => <Outlet />,
  })
  const dockerRoute = createRoute({
    getParentRoute: () => integrationsRoute,
    path: '/docker',
    component: () => <Outlet />,
  })
  const containersRoute = createRoute({
    getParentRoute: () => dockerRoute,
    path: '/containers',
    component: () => <Outlet />,
  })
  const containerDetailRoute = createRoute({
    getParentRoute: () => containersRoute,
    path: '/$name',
    component: () => <ProbesBadge containerName={containerName} />,
  })
  const probesRoute = createRoute({
    getParentRoute: () => containerDetailRoute,
    path: '/probes',
    component: () => <div data-testid="probes-page">{containerName} probes</div>,
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      integrationsRoute.addChildren([
        dockerRoute.addChildren([
          containersRoute.addChildren([containerDetailRoute.addChildren([probesRoute])]),
        ]),
      ]),
    ]),
    history: createMemoryHistory({
      initialEntries: [`/integrations/docker/containers/${containerName}`],
    }),
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
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
    } as unknown as ReturnType<typeof useProbesSummary>)
  })

  it('renders dash when isPending is true', async () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: true,
      isFetching: false,
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('homeassistant')
    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('renders dash when no entry exists', async () => {
    vi.mocked(useProbesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeProbeSummary(),
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('nonexistent')
    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('renders count link when active > 0 and no config errors', async () => {
    renderWithRouter('homeassistant')
    const link = await screen.findByRole('link')
    expect(link).toHaveTextContent('2 active')
    expect(link).toHaveAttribute('aria-label', 'View probes for homeassistant: 2 active')
  })

  it('renders count link with failing probes when failing > 0', async () => {
    renderWithRouter('pihole')
    const link = await screen.findByRole('link')
    expect(link).toHaveTextContent('1 active, 1 failing')
  })

  it('renders dash when active === 0', async () => {
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
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('inactive')
    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('renders red Config error badge when config_errors is non-empty', async () => {
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
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('homeassistant')
    const link = await screen.findByRole('link')
    expect(link).toHaveTextContent('Config error')
    expect(link).toHaveClass('bg-red-50', 'text-red-800')
    expect(link).toHaveAttribute('title', '2 validation errors — click to view')
  })

  it('Config error badge takes priority over active count', async () => {
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
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('homeassistant')
    expect(await screen.findByText('Config error')).toBeInTheDocument()
    expect(screen.queryByText(/active/)).not.toBeInTheDocument()
  })

  it('mounts the link with correct route params', async () => {
    renderWithRouter('pihole')
    const link = await screen.findByRole('link')
    expect(link).toHaveAttribute(
      'href',
      expect.stringContaining('/integrations/docker/containers/pihole/probes'),
    )
  })

  it('renders config error aria-label with all errors', async () => {
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
    } as unknown as ReturnType<typeof useProbesSummary>)
    renderWithRouter('homeassistant')
    const link = await screen.findByRole('link')
    expect(link).toHaveAttribute('aria-label', 'Config error for homeassistant: Error A; Error B')
  })
})
