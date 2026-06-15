import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantCadence } from '@/api/home_assistant'

import { HaCadenceAutomationsDrill } from './HaCadenceAutomationsDrill'
import type { HaCadenceResponse } from './types'

vi.mock('@/api/home_assistant')

afterEach(() => {
  cleanup()
})

function makeResult(
  overrides: Partial<UseQueryResult<HaCadenceResponse, ApiError>>,
): UseQueryResult<HaCadenceResponse, ApiError> {
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
  } as UseQueryResult<HaCadenceResponse, ApiError>
}

function emptyCadence(overrides: Partial<HaCadenceResponse> = {}): HaCadenceResponse {
  return {
    automations: [],
    scripts: [],
    automations_total: 0,
    automations_returned: 0,
    scripts_total: 0,
    scripts_returned: 0,
    filtered_to: 'idle_24h',
    ...overrides,
  }
}

describe('HaCadenceAutomationsDrill', () => {
  it('renders friendly_name primary and age', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({
        data: emptyCadence({
          automations: [
            {
              entity_id: 'automation.lights',
              enabled: true,
              last_triggered_age_seconds: 7200,
              friendly_name: 'Lights Routine',
            },
          ],
          automations_total: 1,
          automations_returned: 1,
        }),
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaCadenceAutomationsDrill />)
    expect(screen.getByText('Lights Routine')).toBeInTheDocument()
    expect(screen.queryByText('disabled')).not.toBeInTheDocument()
    expect(screen.getByText('automation.lights')).toBeInTheDocument()
    expect(screen.getByText('2h ago')).toBeInTheDocument()
  })

  it('renders "never" when last_triggered_age_seconds is null and no disabled badge when enabled', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({
        data: emptyCadence({
          automations: [
            {
              entity_id: 'automation.never',
              enabled: true,
              last_triggered_age_seconds: null,
              friendly_name: null,
            },
          ],
          automations_total: 1,
          automations_returned: 1,
        }),
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaCadenceAutomationsDrill />)
    expect(screen.getByText('automation.never')).toBeInTheDocument()
    expect(screen.getByText('never')).toBeInTheDocument()
    expect(screen.queryByText('disabled')).not.toBeInTheDocument()
  })

  it('renders the empty label when there are no automations', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({ data: emptyCadence(), isSuccess: true, status: 'success' }),
    )
    render(<HaCadenceAutomationsDrill />)
    expect(screen.getByText('No idle automations')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(makeResult({ isPending: true }))
    render(<HaCadenceAutomationsDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaCadenceAutomationsDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
