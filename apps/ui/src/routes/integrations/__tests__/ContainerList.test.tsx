import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ContainerList } from '../ContainerList'
import type { ContainerRow } from '../types'

vi.mock('@/api/docker', () => ({
  useListContainers: vi.fn(),
  useImageUpdatesSummary: vi.fn(() => ({
    data: { byContainer: {}, rateLimitSkippedCount: 0, rateLimitRemainingByRegistry: {} },
    isPending: false,
    isFetching: false,
    isLoading: false,
    error: null,
  })),
  useProbesSummary: vi.fn(() => ({
    data: {},
    isPending: false,
    error: null,
  })),
  dockerImageUpdateQueryKeys: {
    summary: ['integrations', 'docker', 'image-updates-summary'],
  },
}))

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({ children, ...props }: { children: React.ReactNode; [k: string]: unknown }) => (
      <a href={typeof props.to === 'string' ? props.to : '#'}>{children}</a>
    ),
  }
})

import { useListContainers } from '@/api/docker'

const mockContainer = (overrides?: Partial<ContainerRow>): ContainerRow => ({
  id: 'id-test',
  name: 'test-container',
  status: 'running',
  image: 'test-image:latest',
  cpu_pct: 10.5,
  mem_mib: 256,
  compose_file_path: '/home/user/docker-compose.yml',
  compose_service: 'test-service',
  compose_project: null,
  healthcheck: null,
  restart_count: 5,
  restart_count_24h: 1,
  exit_code: null,
  recreated_at: '2024-01-01T00:00:00Z',
  labels: {},
  container_id: null,
  logical_key: null,
  logical_key_kind: null,
  network_mode: null,
  previous_container_id: null,
  ...overrides,
})

describe('ContainerList', () => {
  it('renders containers grouped by compose basename', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: {
        containers: [
          mockContainer({
            id: 'id-1',
            name: 'web',
            compose_file_path: '/home/user/docker-compose.yml',
          }),
          mockContainer({
            id: 'id-2',
            name: 'db',
            compose_file_path: '/home/user/docker-compose.yml',
          }),
        ],
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    expect(screen.getByText('user')).toBeInTheDocument()
  })

  it('renders ungrouped containers under Ungrouped header', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: {
        containers: [mockContainer({ id: 'id-3', name: 'orphan', compose_file_path: null })],
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    expect(screen.getByText('Ungrouped')).toBeInTheDocument()
  })

  it('renders empty state when no containers', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: { containers: [] },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    expect(screen.getByText('No containers found.')).toBeInTheDocument()
  })

  it('shows loading message when containers are pending', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: undefined,
      isPending: true,
      isError: false,
      error: null,
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    expect(screen.getByText('Loading containers…')).toBeInTheDocument()
  })

  it('shows 503 degraded banner when error status is 503', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: { status: 503, message: 'Service unavailable' },
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    expect(screen.getByText('Container data temporarily unavailable')).toBeInTheDocument()
  })

  it('sorts containers alphabetically within groups', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: {
        containers: [
          mockContainer({
            id: 'id-4',
            name: 'zebra',
            compose_file_path: '/home/user/docker-compose.yml',
          }),
          mockContainer({
            id: 'id-5',
            name: 'alpha',
            compose_file_path: '/home/user/docker-compose.yml',
          }),
        ],
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    const alphaEl = screen.getByText('alpha')
    const zebraEl = screen.getByText('zebra')
    expect(alphaEl.compareDocumentPosition(zebraEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows ErrorDisplay for non-503 errors', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: { status: 500, code: 'server_error', message: 'Internal server error' },
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ContainerList />
      </QueryClientProvider>,
    )
    // ErrorDisplay renders the error message
    expect(screen.getByText(/Internal server error/i)).toBeInTheDocument()
  })
})
