import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantUpdates } from '@/api/home_assistant'

import { HaUpdatesDrill } from './HaUpdatesDrill'
import type { HaUpdateRowsResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaUpdateRowsResponse, ApiError>>,
): UseQueryResult<HaUpdateRowsResponse, ApiError> {
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
  } as UseQueryResult<HaUpdateRowsResponse, ApiError>
}

describe('HaUpdatesDrill', () => {
  it('renders the update title', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: {
          updates: [
            {
              entity_id: 'update.router',
              title: 'Router firmware 2.1',
              installed_version: null,
              latest_version: null,
              release_url: null,
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
    render(<HaUpdatesDrill />)
    expect(screen.getByText('Router firmware 2.1')).toBeInTheDocument()
  })

  it('renders the version transition and release link', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: {
          updates: [
            {
              entity_id: 'update.router',
              title: 'Router firmware',
              installed_version: '2.0.1',
              latest_version: '2.1.0',
              release_url: 'https://example.com/notes',
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
    render(<HaUpdatesDrill />)
    expect(screen.getByText('2.0.1 → 2.1.0')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /release notes/i })).toHaveAttribute(
      'href',
      'https://example.com/notes',
    )
  })

  it('renders only the present version with no arrow when one side is null', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: {
          updates: [
            {
              entity_id: 'update.router',
              title: 'Router firmware',
              installed_version: null,
              latest_version: '2.1.0',
              release_url: null,
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
    render(<HaUpdatesDrill />)
    expect(screen.getByText('2.1.0')).toBeInTheDocument()
    expect(screen.queryByText(/→/)).not.toBeInTheDocument()
  })

  it('renders neither arrow nor link when version fields and release_url are null', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: {
          updates: [
            {
              entity_id: 'update.x',
              title: 'Thing',
              installed_version: null,
              latest_version: null,
              release_url: null,
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
    render(<HaUpdatesDrill />)
    expect(screen.getByText('Thing')).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.queryByText(/→/)).not.toBeInTheDocument()
  })

  it('falls back to entity_id when the title is empty', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: {
          updates: [
            {
              entity_id: 'update.nas',
              title: '   ',
              installed_version: null,
              latest_version: null,
              release_url: null,
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
    render(<HaUpdatesDrill />)
    // entity_id appears both as the fallback label and the secondary span.
    expect(screen.getAllByText('update.nas').length).toBeGreaterThan(0)
  })

  it('renders the empty label when there are no updates', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({
        data: { updates: [], filtered_to: null, returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaUpdatesDrill />)
    expect(screen.getByText('No updates pending')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(makeResult({ isPending: true }))
    render(<HaUpdatesDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantUpdates).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaUpdatesDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
