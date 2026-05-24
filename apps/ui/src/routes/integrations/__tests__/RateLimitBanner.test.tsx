import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RateLimitBanner } from '@/routes/integrations/RateLimitBanner'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/docker', () => ({
  useImageUpdatesSummary: vi.fn(),
}))

import { useImageUpdatesSummary } from '@/api/docker'

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RateLimitBanner', () => {
  it('renders nothing when rateLimitSkippedCount === 0', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        byContainer: {},
        rateLimitSkippedCount: 0,
        rateLimitRemainingByRegistry: {},
      },
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    const { container } = render(<RateLimitBanner />)
    expect(container.firstChild).toBeNull()
  })

  it('renders banner when rateLimitSkippedCount > 0', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        byContainer: {},
        rateLimitSkippedCount: 3,
        rateLimitRemainingByRegistry: { 'docker.io': 42 },
      },
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    render(<RateLimitBanner />)
    expect(screen.getByTestId('image-update-rate-limit-banner')).toBeInTheDocument()
    expect(screen.getByTestId('image-update-rate-limit-banner')).toHaveAttribute('role', 'status')
  })

  it('singular vs plural copy for 1 container', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        byContainer: {},
        rateLimitSkippedCount: 1,
        rateLimitRemainingByRegistry: {},
      },
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    render(<RateLimitBanner />)
    expect(screen.getByText(/1 container skipped/)).toBeInTheDocument()
  })

  it('plural copy for multiple containers', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: false,
      isFetching: false,
      isLoading: false,
      error: null,
      data: {
        byContainer: {},
        rateLimitSkippedCount: 5,
        rateLimitRemainingByRegistry: {},
      },
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    render(<RateLimitBanner />)
    expect(screen.getByText(/5 containers skipped/)).toBeInTheDocument()
  })

  it('renders nothing when data undefined (still loading)', () => {
    vi.mocked(useImageUpdatesSummary).mockReturnValue({
      isPending: true,
      isFetching: true,
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof useImageUpdatesSummary>)
    const { container } = render(<RateLimitBanner />)
    expect(container.firstChild).toBeNull()
  })
})
