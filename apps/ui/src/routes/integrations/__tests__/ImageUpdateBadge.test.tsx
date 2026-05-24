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

import { ImageUpdateBadge } from '@/routes/integrations/ImageUpdateBadge'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/docker', () => ({
  useImageUpdatesSummary: vi.fn(),
  dockerImageUpdateQueryKeys: {
    summary: ['integrations', 'docker', 'image-updates-summary'] as const,
  },
}))

import { useImageUpdatesSummary } from '@/api/docker'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeImageUpdateSummary(
  overrides: Record<
    string,
    {
      container_name: string
      available: boolean
      current_digest?: string | null
      latest_digest?: string | null
      last_checked_at?: string | null
      check_error_reason?: string | null
    }
  > = {},
) {
  return {
    byContainer: {
      postgres: {
        container_name: 'postgres',
        available: false,
        current_digest: 'sha256:abc123',
        latest_digest: 'sha256:abc123',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_error_reason: null,
      },
      nginx: {
        container_name: 'nginx',
        available: true,
        current_digest: 'sha256:def456',
        latest_digest: 'sha256:ghi789',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_error_reason: null,
      },
      ...overrides,
    },
    rateLimitSkippedCount: 0,
    rateLimitRemainingByRegistry: {},
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
    component: () => <ImageUpdateBadge containerName={containerName} />,
  })
  const imageUpdateRoute = createRoute({
    getParentRoute: () => containerDetailRoute,
    path: '/image-update',
    component: () => <div data-testid="image-update-page">{containerName} image-update</div>,
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      integrationsRoute.addChildren([
        dockerRoute.addChildren([
          containersRoute.addChildren([containerDetailRoute.addChildren([imageUpdateRoute])]),
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

describe('ImageUpdateBadge', () => {
  beforeEach(() => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeImageUpdateSummary(),
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
  })

  it('renders dash when isPending is true', async () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: true,
      isFetching: false,
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    renderWithRouter('postgres')
    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('renders dash when no entry exists', async () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeImageUpdateSummary(),
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    renderWithRouter('nonexistent')
    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('renders blue Update available link when available=true', async () => {
    renderWithRouter('nginx')
    const link = await screen.findByRole('link')
    expect(link).toHaveTextContent('Update available')
    expect(link).toHaveClass('bg-blue-50', 'text-blue-800')
  })

  it('renders "check failed" when check_error_reason set and available=false', async () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: makeImageUpdateSummary({
        postgres: {
          container_name: 'postgres',
          available: false,
          current_digest: null,
          latest_digest: null,
          last_checked_at: null,
          check_error_reason: 'network_error',
        },
      }),
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    renderWithRouter('postgres')
    const span = await screen.findByText('check failed')
    expect(span).toBeInTheDocument()
    expect(span).toHaveAttribute('title', 'Last check failed: network_error')
  })

  it('renders "up to date" when no error and available=false', async () => {
    renderWithRouter('postgres')
    const span = await screen.findByText('up to date')
    expect(span).toBeInTheDocument()
  })

  it('correct aria-label on Update available link', async () => {
    renderWithRouter('nginx')
    const link = await screen.findByRole('link')
    expect(link).toHaveAttribute('aria-label', 'Update available for nginx')
  })

  it('Update available link has correct href', async () => {
    renderWithRouter('nginx')
    const link = await screen.findByRole('link')
    expect(link).toHaveAttribute(
      'href',
      expect.stringContaining('/integrations/docker/containers/nginx/image-update'),
    )
  })
})
