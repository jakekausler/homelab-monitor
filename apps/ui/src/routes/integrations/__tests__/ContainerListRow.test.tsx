import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ContainerListRow } from '../ContainerListRow'
import type { ContainerRow } from '../types'

vi.mock('@/api/docker', () => ({
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
    Link: ({
      children,
      to,
      params,
      ...props
    }: {
      children: React.ReactNode
      to: string
      params?: Record<string, string>
      [k: string]: unknown
    }) => {
      let href = to
      if (params) {
        for (const [k, v] of Object.entries(params)) {
          href = href.replace('$' + k, v)
        }
      }
      // Remove `params` from spread so React doesn't warn about unknown DOM attribute.
      return (
        <a href={href} {...props}>
          {children}
        </a>
      )
    },
  }
})

function renderRow(container: ContainerRow) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ContainerListRow container={container} />
    </QueryClientProvider>,
  )
}

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

describe('ContainerListRow', () => {
  it('renders container name and image', () => {
    const container = mockContainer({
      name: 'my-web-app',
      image: 'nginx:1.25.0',
    })
    renderRow(container)
    expect(screen.getByText('my-web-app')).toBeInTheDocument()
    expect(screen.getByText(/nginx/)).toBeInTheDocument()
  })

  it('renders status badge when present', () => {
    const container = mockContainer({ status: 'running' })
    renderRow(container)
    expect(screen.getAllByText('Running').length).toBeGreaterThanOrEqual(1)
  })

  it('renders restart chip only when restart_count_24h > 0', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ContainerListRow container={mockContainer({ restart_count_24h: 2 })} />
      </QueryClientProvider>,
    )
    expect(screen.getByText('2 restarts (24h)')).toBeInTheDocument()

    rerender(
      <QueryClientProvider client={qc}>
        <ContainerListRow container={mockContainer({ restart_count_24h: 0 })} />
      </QueryClientProvider>,
    )
    expect(screen.queryByText('2 restarts (24h)')).not.toBeInTheDocument()
  })

  it('renders CPU and memory metrics', () => {
    const container = mockContainer({ cpu_pct: 15.5, mem_mib: 512 })
    renderRow(container)
    expect(screen.getByText('15.5%')).toBeInTheDocument()
    expect(screen.getByText('512 MiB')).toBeInTheDocument()
  })

  it('renders as a clickable link', () => {
    const container = mockContainer({ name: 'clickable-test' })
    renderRow(container)
    const links = screen.getAllByRole('link')
    const rowLink = links.find((l) =>
      l.getAttribute('href')?.includes('/containers/clickable-test'),
    )
    expect(rowLink).toBeDefined()
    expect(rowLink).toBeInTheDocument()
  })

  it('renders singular "1 restart (24h)" when restart_count_24h is 1', () => {
    renderRow(mockContainer({ restart_count_24h: 1 }))
    expect(screen.getAllByText('1 restart (24h)').length).toBeGreaterThanOrEqual(1)
  })
})
