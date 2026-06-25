import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useRecentBlocked } from '@/api/pihole'
import { PiholeRecentBlockedWidget } from './PiholeRecentBlockedWidget'

vi.mock('@/api/pihole')

function ok<T>(data: T): UseQueryResult<T, ApiError> {
  return {
    data,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: true,
    status: 'success',
  } as UseQueryResult<T, ApiError>
}

function err<T = never>(status: number): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<T, ApiError>
}

function pending<T = never>(): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<T, ApiError>
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('PiholeRecentBlockedWidget', () => {
  it('shows loading when pending', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(pending())

    render(<PiholeRecentBlockedWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows yellow banner when 502', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(err(502))

    render(<PiholeRecentBlockedWidget />)
    expect(screen.getByText('Pi-hole recent-blocked temporarily unavailable')).toBeInTheDocument()
  })

  it('shows error display when non-502 error', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(err(500))

    render(<PiholeRecentBlockedWidget />)
    expect(screen.getByText(/Internal error/i)).toBeInTheDocument()
  })

  it('shows empty state when no rows', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(ok({ rows: [], returned: 0 }))

    render(<PiholeRecentBlockedWidget />)
    expect(screen.getByTestId('pihole-recent-blocked-empty')).toBeInTheDocument()
    expect(screen.getByText('No recently blocked domains')).toBeInTheDocument()
  })

  it('renders domain list', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(
      ok({
        rows: ['ads.example.com', 'tracker.test'],
        returned: 2,
      }),
    )

    render(<PiholeRecentBlockedWidget />)
    expect(screen.getByText('ads.example.com')).toBeInTheDocument()
    expect(screen.getByText('tracker.test')).toBeInTheDocument()
  })

  it('renders duplicate domains twice with index key', () => {
    vi.mocked(useRecentBlocked).mockReturnValue(
      ok({
        rows: ['x.com', 'x.com'],
        returned: 2,
      }),
    )

    render(<PiholeRecentBlockedWidget />)
    const elements = screen.getAllByText('x.com')
    expect(elements).toHaveLength(2)
  })
})
