import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { PendingSuggestionsPanel } from './PendingSuggestionsPanel'
import type { DockerSuggestionRow } from './types'

afterEach(() => {
  cleanup()
})

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

vi.mock('@/api/docker', () => ({
  useListDockerSuggestions: vi.fn(),
  dockerQueryKeys: {},
  DockerSuggestionStatus: {},
}))

function _makeMockSuggestion(overrides: Partial<DockerSuggestionRow> = {}): DockerSuggestionRow {
  return {
    id: 'suggestion-default',
    container_id: 'container-default',
    container_name: 'default-service',
    image_ref: 'default:latest',
    labels: {},
    detection_reason: 'no_homelab_monitor_label',
    kind: 'docker_container_discovered',
    state: 'pending',
    deduplication_key: 'container-default',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    compose_project: null,
    compose_service: null,
    compose_file_path: null,
    ...overrides,
  }
}

describe('PendingSuggestionsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty state when zero suggestions', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: { pages: [{ suggestions: [], next_cursor: null }], pageParams: [undefined] },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByText('Pending suggestions (0)')).toBeInTheDocument()
    expect(screen.getByTestId('pending-suggestions-empty')).toBeInTheDocument()
    expect(screen.getByText('No pending suggestions.')).toBeInTheDocument()
  })

  it('renders list of pending suggestions', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'web-app',
        image_ref: 'nginx:latest',
        labels: { app: 'web' },
      }),
      _makeMockSuggestion({
        id: 'suggestion-2',
        container_name: 'database',
        image_ref: 'postgres:15',
        labels: { env: 'prod' },
        detection_reason: 'disabled_profile',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByText('Pending suggestions (2)')).toBeInTheDocument()
    expect(screen.getByText('web-app')).toBeInTheDocument()
    expect(screen.getByText('database')).toBeInTheDocument()
    expect(screen.getByText('nginx:latest')).toBeInTheDocument()
    expect(screen.getByText('postgres:15')).toBeInTheDocument()
  })

  it('renders disabled-profile pill when detection_reason is disabled_profile', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'disabled-service',
        image_ref: 'service:latest',
        detection_reason: 'disabled_profile',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByText('Disabled profile')).toBeInTheDocument()
  })

  it('renders no-homelab-label pill when detection_reason is no_homelab_monitor_label', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'no-label-service',
        image_ref: 'service:latest',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByText('No labels')).toBeInTheDocument()
  })

  it('renders label-collision pill when kind is docker_label_collision', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'collision-service',
        image_ref: 'service:latest',
        detection_reason: 'label_collision',
        kind: 'docker_label_collision',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByTestId('collision-pill')).toBeInTheDocument()
    expect(screen.getByText('Label collision')).toBeInTheDocument()
  })

  it('accept customize ignore buttons are present but disabled', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'test-service',
        image_ref: 'service:latest',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    const acceptBtn = screen.getByRole('button', { name: 'Accept' })
    const customizeBtn = screen.getByRole('button', { name: 'Customize' })
    const ignoreBtn = screen.getByRole('button', { name: 'Ignore' })

    expect(acceptBtn).toBeDisabled()
    expect(customizeBtn).toBeDisabled()
    expect(ignoreBtn).toBeDisabled()
    expect(acceptBtn).toHaveAttribute('aria-disabled', 'true')
    expect(customizeBtn).toHaveAttribute('aria-disabled', 'true')
    expect(ignoreBtn).toHaveAttribute('aria-disabled', 'true')
  })

  it('load more button appears only when next_cursor is non-null', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'test-service',
        image_ref: 'service:latest',
      }),
    ]

    // First render: hasNextPage = false
    const { rerender } = renderWithQueryClient(<PendingSuggestionsPanel />)
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)
    rerender(<PendingSuggestionsPanel />)
    expect(screen.queryByTestId('suggestions-load-more')).not.toBeInTheDocument()

    // Second render: hasNextPage = true
    cleanup()
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: 'cursor-123' }],
        pageParams: [undefined],
      },
      hasNextPage: true,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)
    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByTestId('suggestions-load-more')).toBeInTheDocument()
    expect(screen.getByText('Load more')).toBeInTheDocument()
  })

  it('load more fetches next page on click', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockFetchNextPage = vi.fn()
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'test-service',
        image_ref: 'service:latest',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: 'cursor-123' }],
        pageParams: [undefined],
      },
      hasNextPage: true,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: mockFetchNextPage,
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    const loadMoreBtn = screen.getByTestId('suggestions-load-more')
    await userEvent.click(loadMoreBtn)
    expect(mockFetchNextPage).toHaveBeenCalledOnce()
  })

  it('header text includes pending count', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    const mockSuggestions: DockerSuggestionRow[] = [
      _makeMockSuggestion({
        id: 'suggestion-1',
        container_name: 'service-1',
        image_ref: 'service:latest',
      }),
      _makeMockSuggestion({
        id: 'suggestion-2',
        container_name: 'service-2',
        image_ref: 'service:latest',
        detection_reason: 'disabled_profile',
      }),
      _makeMockSuggestion({
        id: 'suggestion-3',
        container_name: 'service-3',
        image_ref: 'service:latest',
      }),
    ]
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: mockSuggestions, next_cursor: null }],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByText('Pending suggestions (3)')).toBeInTheDocument()
  })

  it('renders compose_file_path when present', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [
          {
            suggestions: [
              _makeMockSuggestion({
                compose_file_path: '/storage/docker/compose/docker-compose.yml',
              }),
            ],
            next_cursor: null,
          },
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.getByTestId('compose-file-path')).toBeInTheDocument()
    expect(screen.getByText('/storage/docker/compose/docker-compose.yml')).toBeInTheDocument()
  })

  it('does not render compose_file_path when null', async () => {
    const { useListDockerSuggestions } = await import('@/api/docker')
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [
          {
            suggestions: [_makeMockSuggestion({ compose_file_path: null })],
            next_cursor: null,
          },
        ],
        pageParams: [undefined],
      },
      hasNextPage: false,
      isFetchingNextPage: false,
      isLoading: false,
      isError: false,
      fetchNextPage: vi.fn(),
    } as unknown as ReturnType<typeof useListDockerSuggestions>)

    renderWithQueryClient(<PendingSuggestionsPanel />)
    expect(screen.queryByTestId('compose-file-path')).not.toBeInTheDocument()
  })
})
