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
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ContainerImageUpdatePage } from '@/routes/integrations/ContainerImageUpdatePage'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/docker', () => ({
  useImageUpdate: vi.fn(),
  useImageUpdatesSummary: () => ({ data: null }),
}))

import { useImageUpdate } from '@/api/docker'

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
  const imageUpdateRoute = createRoute({
    getParentRoute: () => dockerRoute,
    path: '/containers/$name/image-update',
    component: ContainerImageUpdatePage,
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      integrationsRoute.addChildren([dockerRoute.addChildren([imageUpdateRoute])]),
    ]),
    history: createMemoryHistory({
      initialEntries: [`/integrations/docker/containers/${containerName}/image-update`],
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

describe('ContainerImageUpdatePage', () => {
  it('renders loading state when isPending', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: true,
      isFetching: true,
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    expect(await screen.findByText('Loading image-update state…')).toBeInTheDocument()
  })

  it('renders error display on error', async () => {
    const error = new Error('Failed to fetch')
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: true,
      error: error as unknown,
      data: undefined,
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    expect(await screen.findByText('Failed to fetch')).toBeInTheDocument()
  })

  it('renders all dt/dd pairs when data present', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'postgres',
        last_local_digest: 'sha256:abc123',
        last_registry_digest: 'sha256:def456',
        last_image_ref: 'postgres:16',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
        update_available: true,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    expect(await screen.findByText('Image ref')).toBeInTheDocument()
    expect(await screen.findByText('postgres:16')).toBeInTheDocument()
    expect(await screen.findByText('Update available')).toBeInTheDocument()
    expect(await screen.findByText('yes')).toBeInTheDocument()
    expect(await screen.findByText('Current digest')).toBeInTheDocument()
    expect(await screen.findByText('Latest digest')).toBeInTheDocument()
    expect(await screen.findByText('Last checked')).toBeInTheDocument()
    expect(await screen.findByText('Check failed at')).toBeInTheDocument()
  })

  it('renders update_available yes/no correctly', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'nginx',
        last_local_digest: 'sha256:abc123',
        last_registry_digest: 'sha256:abc123',
        last_image_ref: 'nginx:latest',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
        update_available: false,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('nginx')
    expect(await screen.findByText('no')).toBeInTheDocument()
  })

  it('renders check_error_reason in red when present', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'postgres',
        last_local_digest: null,
        last_registry_digest: null,
        last_image_ref: 'postgres:16',
        last_checked_at: null,
        check_failed_at: '2026-05-23T10:00:00Z',
        check_error_reason: 'network_error',
        update_available: false,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    const errorReason = await screen.findByText('network_error')
    expect(errorReason).toBeInTheDocument()
    expect(errorReason).toHaveClass('text-red-700')
  })

  it('does not render check_error_reason block when null', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'postgres',
        last_local_digest: 'sha256:abc123',
        last_registry_digest: 'sha256:abc123',
        last_image_ref: 'postgres:16',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
        update_available: false,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    await screen.findByText('Image ref')
    expect(screen.queryByText('Check error reason')).not.toBeInTheDocument()
  })

  it('renders dashes for null digests', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'postgres',
        last_local_digest: null,
        last_registry_digest: null,
        last_image_ref: 'postgres:16',
        last_checked_at: null,
        check_failed_at: null,
        check_error_reason: null,
        update_available: false,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    const dashes = await screen.findAllByText('—')
    expect(dashes.length).toBeGreaterThan(0)
  })
})
