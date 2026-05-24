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
        source: 'registry',
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
        source: 'registry',
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
        source: 'registry',
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
        source: 'registry',
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
        source: 'registry',
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('postgres')
    const dashes = await screen.findAllByText('—')
    expect(dashes.length).toBeGreaterThan(0)
  })

  // ---- local_build source tests (STAGE-003-009) ----

  it('renders local_build detail section when source=local_build', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: true,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: 'abc123def456abc123def456abc123def456abc123def456abc123def456abc1',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    expect(await screen.findByText('Source')).toBeInTheDocument()
    expect(await screen.findByText('Local build')).toBeInTheDocument()
    expect(await screen.findByText('Compose service')).toBeInTheDocument()
    expect(await screen.findByText('udo-viewer')).toBeInTheDocument()
    expect(await screen.findByText('Build context path')).toBeInTheDocument()
    expect(await screen.findByText('/srv/compose/udo-viewer')).toBeInTheDocument()
    expect(await screen.findByText('Last source hash')).toBeInTheDocument()
  })

  it('renders source hash truncated via formatSourceHash for local_build', async () => {
    const fullHash = 'abc123def456abc123def456abc123def456abc123def456abc123def456abc1'
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: false,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: fullHash,
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    // The formatSourceHash should truncate to 12 chars + ellipsis
    expect(await screen.findByText('abc123def456…')).toBeInTheDocument()
  })

  it('renders registry detail section when source=registry (regression)', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'nginx',
        source: 'registry',
        update_available: false,
        last_image_ref: 'nginx:latest',
        last_local_digest: 'sha256:abc',
        last_registry_digest: 'sha256:abc',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('nginx')
    expect(await screen.findByText('Image ref')).toBeInTheDocument()
    expect(await screen.findByText('nginx:latest')).toBeInTheDocument()
    expect(await screen.findByText('Current digest')).toBeInTheDocument()
    expect(await screen.findByText('Latest digest')).toBeInTheDocument()
  })

  it('does not render registry section when source=local_build', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: false,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: 'abc123',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    await screen.findByText('Source')
    expect(screen.queryByText('Image ref')).not.toBeInTheDocument()
    expect(screen.queryByText('Current digest')).not.toBeInTheDocument()
  })

  // ---- baseline_source_hash display tests (STAGE-003-009 refinement fix) ----

  it('renders Baseline hash row when source=local_build, update_available=true, baseline differs from current', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: true,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: 'newhashabcdef123456',
        baseline_source_hash: 'baselineabc123def456',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    expect(await screen.findByText('Baseline source hash')).toBeInTheDocument()
    // formatSourceHash truncates to 12 chars + ellipsis
    expect(await screen.findByText('baselineabc1…')).toBeInTheDocument()
  })

  it('does NOT render Baseline hash row when source=local_build and update_available=false', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: false,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: 'abc123def456',
        baseline_source_hash: 'abc123def456',
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    await screen.findByText('Source')
    expect(screen.queryByText('Baseline source hash')).not.toBeInTheDocument()
  })

  it('does NOT render Baseline hash row when update_available=true but baseline equals current (defensive)', async () => {
    const sameHash = 'abc123def456abc123def456'
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: true,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: sameHash,
        baseline_source_hash: sameHash,
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    await screen.findByText('Source')
    expect(screen.queryByText('Baseline source hash')).not.toBeInTheDocument()
  })

  it('does NOT render Baseline hash row when update_available=true but baseline_source_hash is null (defensive)', async () => {
    vi.mocked(useImageUpdate).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        container_name: 'udo-viewer',
        source: 'local_build',
        update_available: true,
        compose_service: 'udo-viewer',
        build_context_path: '/srv/compose/udo-viewer',
        last_source_hash: 'newhashabc123',
        baseline_source_hash: null,
        last_checked_at: '2026-05-23T10:00:00Z',
        check_failed_at: null,
        check_error_reason: null,
      },
    } as unknown as ReturnType<typeof useImageUpdate>)
    renderWithRouter('udo-viewer')
    await screen.findByText('Source')
    expect(screen.queryByText('Baseline source hash')).not.toBeInTheDocument()
  })
})
