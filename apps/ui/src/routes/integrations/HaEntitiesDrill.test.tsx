import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantEntities } from '@/api/home_assistant'

import { HaEntitiesDrill } from './HaEntitiesDrill'
import type { HaEntityRowsResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaEntityRowsResponse, ApiError>>,
): UseQueryResult<HaEntityRowsResponse, ApiError> {
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
  } as UseQueryResult<HaEntityRowsResponse, ApiError>
}

describe('HaEntitiesDrill', () => {
  it('renders entity rows with entity_id as primary and suppresses secondary line when friendly_name is null', () => {
    vi.mocked(useHomeAssistantEntities).mockReturnValue(
      makeResult({
        data: {
          entities: [
            {
              entity_id: 'sensor.kitchen',
              domain: 'sensor',
              available: false,
              last_changed_age_seconds: 7200,
              friendly_name: null,
            },
          ],
          filtered_to: 'unavailable',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaEntitiesDrill />)
    expect(screen.getByText('sensor.kitchen')).toBeInTheDocument()
    expect(screen.queryByText('sensor')).not.toBeInTheDocument()
    expect(screen.getByText('2h ago')).toBeInTheDocument()
  })

  it('renders friendly_name as primary and shows entity_id · domain secondary line when friendly_name is set', () => {
    vi.mocked(useHomeAssistantEntities).mockReturnValue(
      makeResult({
        data: {
          entities: [
            {
              entity_id: 'sensor.kitchen',
              domain: 'sensor',
              available: false,
              last_changed_age_seconds: 7200,
              friendly_name: 'Kitchen Sensor',
            },
          ],
          filtered_to: 'unavailable',
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaEntitiesDrill />)
    expect(screen.getByText('Kitchen Sensor')).toBeInTheDocument()
    expect(screen.getByText('sensor.kitchen · sensor')).toBeInTheDocument()
    expect(screen.getByText('2h ago')).toBeInTheDocument()
  })

  it('renders the empty label when there are no entities', () => {
    vi.mocked(useHomeAssistantEntities).mockReturnValue(
      makeResult({
        data: { entities: [], filtered_to: 'unavailable', returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaEntitiesDrill />)
    expect(screen.getByText('No unavailable entities')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantEntities).mockReturnValue(makeResult({ isPending: true }))
    render(<HaEntitiesDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantEntities).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaEntitiesDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
