import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantBatteries } from '@/api/home_assistant'

import { HaBatteriesDrill } from './HaBatteriesDrill'
import type { HaBatteryRowsResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaBatteryRowsResponse, ApiError>>,
): UseQueryResult<HaBatteryRowsResponse, ApiError> {
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
  } as UseQueryResult<HaBatteryRowsResponse, ApiError>
}

describe('HaBatteriesDrill', () => {
  it('renders battery rows with entity_id and level percent', () => {
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(
      makeResult({
        data: {
          batteries: [{ entity_id: 'sensor.door', domain: 'sensor', level: 42, device: null }],
          filtered_to: 'low_or_critical',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaBatteriesDrill />)
    expect(screen.getByText('sensor.door')).toBeInTheDocument()
    expect(screen.getByText('42%')).toBeInTheDocument()
  })

  it('tints critical level (<10) red', () => {
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(
      makeResult({
        data: {
          batteries: [{ entity_id: 'sensor.a', domain: 'sensor', level: 5, device: null }],
          filtered_to: 'low_or_critical',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaBatteriesDrill />)
    expect(screen.getByText('5%').className).toContain('text-red-700')
  })

  it('tints low level (10–19) amber', () => {
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(
      makeResult({
        data: {
          batteries: [{ entity_id: 'sensor.b', domain: 'sensor', level: 15, device: null }],
          filtered_to: 'low_or_critical',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaBatteriesDrill />)
    expect(screen.getByText('15%').className).toContain('text-amber-700')
  })

  it('renders the empty label when there are no batteries', () => {
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(
      makeResult({
        data: { batteries: [], filtered_to: 'low_or_critical', returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaBatteriesDrill />)
    expect(screen.getByText('All batteries healthy')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(makeResult({ isPending: true }))
    render(<HaBatteriesDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantBatteries).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaBatteriesDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
