import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantCadence } from '@/api/home_assistant'

import { HaCadenceScriptsDrill } from './HaCadenceScriptsDrill'
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

describe('HaCadenceScriptsDrill', () => {
  it('renders a script row with friendly_name primary and formatted age', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({
        data: emptyCadence({
          scripts: [
            {
              entity_id: 'script.backup',
              last_triggered_age_seconds: 90000,
              friendly_name: 'Backup Job',
            },
          ],
          scripts_total: 1,
          scripts_returned: 1,
        }),
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaCadenceScriptsDrill />)
    expect(screen.getByText('Backup Job')).toBeInTheDocument()
    expect(screen.getByText('script.backup')).toBeInTheDocument()
    expect(screen.getByText('1d ago')).toBeInTheDocument()
  })

  it('renders "never" when last_triggered_age_seconds is null', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({
        data: emptyCadence({
          scripts: [
            {
              entity_id: 'script.unused',
              last_triggered_age_seconds: null,
              friendly_name: null,
            },
          ],
          scripts_total: 1,
          scripts_returned: 1,
        }),
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaCadenceScriptsDrill />)
    expect(screen.getByText('script.unused')).toBeInTheDocument()
    expect(screen.getByText('never')).toBeInTheDocument()
  })

  it('renders the empty label when there are no scripts', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({ data: emptyCadence(), isSuccess: true, status: 'success' }),
    )
    render(<HaCadenceScriptsDrill />)
    expect(screen.getByText('No idle scripts')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantCadence).mockReturnValue(makeResult({ isPending: true }))
    render(<HaCadenceScriptsDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantCadence).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaCadenceScriptsDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })
})
