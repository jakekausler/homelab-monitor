import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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

vi.mock('@/lib/sourceHash', () => ({
  formatSourceHash: (v: string | null | undefined) => (v ? `[${v.slice(0, 6)}]` : '—'),
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
      source?: string
      current_digest?: string | null
      latest_digest?: string | null
      last_checked_at?: string | null
      check_error_reason?: string | null
      last_source_hash?: string | null
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
// Render helper
// ---------------------------------------------------------------------------

function renderBadge(containerName: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ImageUpdateBadge containerName={containerName} />
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
      isError: false,
      error: null,
      data: makeImageUpdateSummary(),
    } as never)
  })

  it('renders nothing when isPending is true', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: true,
      isFetching: false,
      isLoading: true,
      isError: false,
      error: null,
      data: undefined,
    } as never)
    const { container } = renderBadge('postgres')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when isError is true', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: true,
      error: new Error('failed'),
      data: undefined,
    } as never)
    const { container } = renderBadge('postgres')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when no entry exists for container', () => {
    const { container } = renderBadge('nonexistent')
    expect(container).toBeEmptyDOMElement()
  })

  it('renders "Update available" badge when available=true and source=registry', () => {
    renderBadge('nginx')
    expect(screen.getByText('Update available')).toBeInTheDocument()
  })

  it('correct aria-label on Update available badge', () => {
    renderBadge('nginx')
    expect(screen.getByLabelText('Update available for nginx')).toBeInTheDocument()
  })

  it('renders "Update Check Failed" badge when check_error_reason set and available=false', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: false,
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
    } as never)
    renderBadge('postgres')
    expect(screen.getByText('Update Check Failed')).toBeInTheDocument()
    expect(screen.getByLabelText('Update Check Failed')).toBeInTheDocument()
  })

  it('renders "Up to date" badge when no error and available=false', () => {
    renderBadge('postgres')
    expect(screen.getByText('Up to date')).toBeInTheDocument()
    expect(screen.getByLabelText('Image up to date')).toBeInTheDocument()
  })

  it('renders "Rebuild needed" badge when source=local_build and available=true', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: false,
      error: null,
      data: makeImageUpdateSummary({
        'udo-viewer': {
          container_name: 'udo-viewer',
          available: true,
          source: 'local_build',
          last_source_hash: 'abc123def456abc1',
        },
      }),
    } as never)
    renderBadge('udo-viewer')
    expect(screen.getByText('Rebuild needed')).toBeInTheDocument()
  })

  it('correct aria-label on Rebuild needed badge', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: false,
      error: null,
      data: makeImageUpdateSummary({
        'udo-viewer': {
          container_name: 'udo-viewer',
          available: true,
          source: 'local_build',
          last_source_hash: 'abc123def456abc1',
        },
      }),
    } as never)
    renderBadge('udo-viewer')
    expect(
      screen.getByLabelText('Source changed — rebuild needed for udo-viewer'),
    ).toBeInTheDocument()
  })

  it('renders "Up to date" when source=local_build and available=false (no error)', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      isError: false,
      error: null,
      data: makeImageUpdateSummary({
        'udo-viewer': {
          container_name: 'udo-viewer',
          available: false,
          source: 'local_build',
          current_digest: null,
          latest_digest: null,
          last_checked_at: '2026-05-23T10:00:00Z',
          check_error_reason: null,
        },
      }),
    } as never)
    renderBadge('udo-viewer')
    expect(screen.getByText('Up to date')).toBeInTheDocument()
  })

  it('renders "Update available" (not "Rebuild needed") when source=registry and available=true', () => {
    renderBadge('nginx')
    expect(screen.getByText('Update available')).toBeInTheDocument()
    expect(screen.queryByText('Rebuild needed')).not.toBeInTheDocument()
  })
})
