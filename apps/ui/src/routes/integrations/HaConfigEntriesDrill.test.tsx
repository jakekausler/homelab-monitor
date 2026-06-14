import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantConfigEntries } from '@/api/home_assistant'

import { HaConfigEntriesDrill } from './HaConfigEntriesDrill'
import type { HaConfigEntryRowsResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaConfigEntryRowsResponse, ApiError>>,
): UseQueryResult<HaConfigEntryRowsResponse, ApiError> {
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
  } as UseQueryResult<HaConfigEntryRowsResponse, ApiError>
}

describe('HaConfigEntriesDrill', () => {
  it('renders config-entry rows with domain and state badge', () => {
    vi.mocked(useHomeAssistantConfigEntries).mockReturnValue(
      makeResult({
        data: {
          config_entries: [{ domain: 'zwave_js', state: 'setup_error', title: 'Z-Wave JS' }],
          filtered_to: 'error',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaConfigEntriesDrill />)
    expect(screen.getByText('Z-Wave JS')).toBeInTheDocument()
    expect(screen.getByText('zwave_js')).toBeInTheDocument()
    expect(screen.getByText('setup_error')).toBeInTheDocument()
  })

  it('renders the empty label when there are no entries', () => {
    vi.mocked(useHomeAssistantConfigEntries).mockReturnValue(
      makeResult({
        data: { config_entries: [], filtered_to: 'error', returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaConfigEntriesDrill />)
    expect(screen.getByText('No integration errors')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantConfigEntries).mockReturnValue(makeResult({ isPending: true }))
    render(<HaConfigEntriesDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantConfigEntries).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaConfigEntriesDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
