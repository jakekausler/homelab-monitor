import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantRepairs } from '@/api/home_assistant'

import { HaRepairsDrill } from './HaRepairsDrill'
import type { HaRepairRowsResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaRepairRowsResponse, ApiError>>,
): UseQueryResult<HaRepairRowsResponse, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    isFetching: false,
    isLoading: false,
    isLoadingError: false,
    isRefetchError: false,
    isStale: false,
    isPlaceholderData: false,
    dataUpdatedAt: 0,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    fetchStatus: 'idle',
    refetch: vi.fn(),
    status: 'pending',
    ...overrides,
  } as UseQueryResult<HaRepairRowsResponse, ApiError>
}

describe('HaRepairsDrill', () => {
  it('renders repair rows with issue_id, domain, and severity badge', () => {
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(
      makeResult({
        data: {
          repairs: [
            {
              domain: 'cloud',
              issue_id: 'legacy_subscription',
              severity: 'warning',
              description: null,
              learn_more_url: null,
            },
          ],
          filtered_to: null,
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaRepairsDrill />)
    expect(screen.getByText('legacy_subscription')).toBeInTheDocument()
    expect(screen.getByText('cloud')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()
  })

  it('renders the empty label when there are no repairs', () => {
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(
      makeResult({
        data: { repairs: [], filtered_to: null, returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaRepairsDrill />)
    expect(screen.getByText('No active repairs')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(makeResult({ isPending: true }))
    render(<HaRepairsDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaRepairsDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })

  it('renders the description and learn-more link', () => {
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(
      makeResult({
        data: {
          repairs: [
            {
              domain: 'cloud',
              issue_id: 'legacy_subscription',
              severity: 'warning',
              description: 'Your cloud subscription is using a legacy plan.',
              learn_more_url: 'https://example.com/repair',
            },
          ],
          filtered_to: null,
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaRepairsDrill />)
    expect(screen.getByText('Your cloud subscription is using a legacy plan.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /learn more/i })).toHaveAttribute(
      'href',
      'https://example.com/repair',
    )
  })

  it('renders no description line or link when both are null', () => {
    vi.mocked(useHomeAssistantRepairs).mockReturnValue(
      makeResult({
        data: {
          repairs: [
            {
              domain: 'cloud',
              issue_id: 'x',
              severity: 'warning',
              description: null,
              learn_more_url: null,
            },
          ],
          filtered_to: null,
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaRepairsDrill />)
    expect(screen.getByText('x')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })
})
