import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantSummary } from '@/api/home_assistant'
import type { Schema } from '@/api/types'

import { HomeAssistantHealthTab } from './HomeAssistantHealthTab'

vi.mock('@/api/home_assistant')

type HaSummaryResponse = Schema<'HaSummaryResponse'>

function makeResult(
  overrides: Partial<UseQueryResult<HaSummaryResponse, ApiError>>,
): UseQueryResult<HaSummaryResponse, ApiError> {
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
  } as UseQueryResult<HaSummaryResponse, ApiError>
}

const MOCK_DATA: HaSummaryResponse = {
  ha_up: true,
  last_seen: '2026-06-12T00:00:00Z',
  entities: { total: 1906, available: 943, unavailable: 963 },
  battery: { low: 3, critical: 1 },
  updates: { available: 2, total: 8 },
  config_entries: { loaded: 45, error: 0 },
  repairs: 0,
  notifications: 0,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HomeAssistantHealthTab', () => {
  beforeEach(() => {
    vi.mocked(useHomeAssistantSummary).mockReturnValue(
      makeResult({ data: MOCK_DATA, isSuccess: true, status: 'success' }),
    )
  })

  it('renders entity health counts when data is present and ha_up is true', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('1906')).toBeInTheDocument()
    expect(screen.getByText('943')).toBeInTheDocument()
    expect(screen.getByText('963')).toBeInTheDocument()
  })

  it('renders battery counts when data is present and ha_up is true', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('renders no banner when ha_up is true', () => {
    render(<HomeAssistantHealthTab />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows loading text when isPending', () => {
    vi.mocked(useHomeAssistantSummary).mockReturnValue(
      makeResult({ isPending: true, isLoading: true, status: 'pending' }),
    )
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText('1906')).not.toBeInTheDocument()
  })

  it('shows 502 banner when error.status === 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantSummary).mockReturnValue(
      makeResult({ isError: true, error: err, status: 'error' }),
    )
    render(<HomeAssistantHealthTab />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
    expect(screen.queryByText('1906')).not.toBeInTheDocument()
  })

  it('shows ErrorDisplay when error.status !== 502', () => {
    const err = new Error('Server error') as ApiError & { status: number }
    err.status = 500
    vi.mocked(useHomeAssistantSummary).mockReturnValue(
      makeResult({ isError: true, error: err, status: 'error' }),
    )
    render(<HomeAssistantHealthTab />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('1906')).not.toBeInTheDocument()
  })

  it('shows offline banner AND widgets when ha_up is false', () => {
    vi.mocked(useHomeAssistantSummary).mockReturnValue(
      makeResult({
        data: { ...MOCK_DATA, ha_up: false },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HomeAssistantHealthTab />)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByText(/Home Assistant offline/)).toBeInTheDocument()
    // Widgets still rendered with last-known counts
    expect(screen.getByText('1906')).toBeInTheDocument()
  })
})
