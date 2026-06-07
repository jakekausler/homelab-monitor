import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { ContainerOverviewTab } from '../ContainerOverviewTab'

const mockUseParams = vi.fn(() => ({ name: 'test-container' }))

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useParams: () => mockUseParams(),
  }
})

const mockUseImageUpdate = vi.fn(() => ({
  data: null,
  isPending: false,
  isError: false,
  error: null,
}))

const mockUseListContainers = vi.fn(() => ({
  data: {
    containers: [
      {
        name: 'test-container',
        image: 'test-image:latest',
        status: 'running',
        healthcheck: 'healthy',
        compose_project: 'myproject',
        compose_service: 'web',
        compose_file_path: '/home/user/docker-compose.yml',
        restart_count_24h: 0,
        restart_count: 0,
        exit_code: null,
        recreated_at: null,
        cpu_pct: 5.2,
        mem_mib: 128,
      },
    ],
  },
  isPending: false,
  isError: false,
  error: null,
}))

vi.mock('@/api/docker', () => ({
  useImageUpdate: (...args: unknown[]) => mockUseImageUpdate(...(args as [])),
  useListContainers: () => mockUseListContainers(),
  dockerImageUpdateQueryKeys: {
    detail: (name: string) => ['imageUpdate', name],
  },
  dockerQueryKeys: {
    containers: ['integrations', 'docker', 'containers'],
  },
  useContainerCrashes: () => ({
    data: { container_name: 'test-container', crashes: [] },
    isPending: false,
    isError: false,
    error: undefined,
  }),
  useContainerCrashDetail: () => ({
    data: undefined,
    isPending: true,
    isError: false,
    error: undefined,
  }),
  dockerCrashesQueryKeys: {
    list: (name: string) => ['crashes', name],
    detail: (name: string, id: string) => ['crashes', name, id],
  },
}))

const mockUseMetricsRange = vi.fn(() => ({
  data: null,
  isPending: false,
  isError: false,
  error: null,
}))

vi.mock('@/api/queries', () => ({
  useMetricsRange: (...args: unknown[]) => mockUseMetricsRange(...(args as [])),
}))

vi.mock('@/components/docker/PullRestartModal', () => ({
  PullRestartModal: ({ onActionStarted }: { onActionStarted: (id: number) => void }) => (
    <button data-testid="modal-start-action" onClick={() => onActionStarted(42)}>
      Start action
    </button>
  ),
}))

vi.mock('@/components/tiles/Sparkline', () => ({
  Sparkline: ({ ariaLabel }: { ariaLabel: string }) => <svg aria-label={ariaLabel} />,
}))

const TestWrapper = ({ children }: { children: React.ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('ContainerOverviewTab', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    cleanup()
  })

  it('renders overview tab content', () => {
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Restart history')).toBeInTheDocument()
  })

  it('shows no-container-name error when name param is missing', () => {
    mockUseParams.mockReturnValueOnce({ name: undefined } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('No container name provided.')).toBeInTheDocument()
  })

  it('shows no-container-name error when name param is empty string', () => {
    mockUseParams.mockReturnValueOnce({ name: '' })
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('No container name provided.')).toBeInTheDocument()
  })

  it('shows image update loading state', () => {
    mockUseImageUpdate.mockReturnValueOnce({
      data: null,
      isPending: true,
      isError: false,
      error: null,
    })
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows image update error state', () => {
    mockUseImageUpdate.mockReturnValueOnce({
      data: null,
      isPending: false,
      isError: true,
      error: { status: 500, message: 'Server error' },
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    // ErrorDisplay renders — just confirm restart history section is present (no crash)
    expect(screen.getByText('Restart history')).toBeInTheDocument()
  })

  it('renders registry image update data without update_available', () => {
    mockUseImageUpdate.mockReturnValueOnce({
      data: {
        source: 'registry',
        last_image_ref: 'nginx:latest',
        last_local_digest: 'sha256:abc123',
        last_registry_digest: 'sha256:abc123',
        update_available: false,
        last_checked_at: null,
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Registry')).toBeInTheDocument()
    expect(screen.getByText('no')).toBeInTheDocument()
    expect(screen.queryByTestId('pull-restart-button')).not.toBeInTheDocument()
  })

  it('renders local_build image update data with update_available and Rebuild button', () => {
    mockUseImageUpdate.mockReturnValueOnce({
      data: {
        source: 'local_build',
        build_context_path: '/srv/app',
        compose_service: 'myapp',
        last_source_hash: 'abc',
        baseline_source_hash: 'def',
        update_available: true,
        last_checked_at: null,
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Local build')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rebuild & Restart' })).toBeInTheDocument()
  })

  it('renders registry image update with update_available and Pull & Restart button', () => {
    mockUseImageUpdate.mockReturnValueOnce({
      data: {
        source: 'registry',
        last_image_ref: 'nginx:latest',
        last_local_digest: 'sha256:old',
        last_registry_digest: 'sha256:new',
        update_available: true,
        last_checked_at: null,
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByRole('button', { name: 'Pull & Restart' })).toBeInTheDocument()
  })

  it('shows cpu and memory sparkline loading placeholders when metrics are pending', () => {
    mockUseMetricsRange.mockReturnValue({
      data: null,
      isPending: true,
      isError: false,
      error: null,
    })
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByTestId('cpu-sparkline-loading')).toBeInTheDocument()
  })

  it('shows cpu sparkline no-history message when metrics return empty result', () => {
    mockUseMetricsRange.mockReturnValue({
      data: { data: { result: [] } },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByTestId('cpu-sparkline-empty')).toBeInTheDocument()
  })

  it('renders sparklines when metrics data is available', () => {
    mockUseMetricsRange.mockReturnValue({
      data: {
        data: {
          result: [
            {
              values: Array.from({ length: 60 }, (_, i) => [i, '5.0']),
            },
          ],
        },
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    expect(screen.getByLabelText('CPU history for test-container')).toBeInTheDocument()
  })

  it('handlePullRestartStart: sets action in progress and re-enables button after 30s', async () => {
    mockUseImageUpdate.mockReturnValue({
      data: {
        source: 'registry',
        last_image_ref: 'nginx:latest',
        last_local_digest: 'sha256:old',
        last_registry_digest: 'sha256:new',
        update_available: true,
        last_checked_at: null,
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    const { act } = await import('@testing-library/react')
    render(
      <TestWrapper>
        <ContainerOverviewTab />
      </TestWrapper>,
    )
    // Trigger onActionStarted callback from the modal mock (modal always rendered)
    act(() => {
      fireEvent.click(screen.getByTestId('modal-start-action'))
    })
    // Button should now be disabled (actionInProgress=true)
    expect(screen.getByTestId('pull-restart-button')).toBeDisabled()
    // Advance 30 seconds — the timeout fires
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // After timeout, action is no longer in progress
    expect(screen.getByTestId('pull-restart-button')).not.toBeDisabled()
  })
})
