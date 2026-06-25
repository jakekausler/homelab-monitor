import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { MutateOptions, UseMutationResult, UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { ApiError as RealApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useBlockingMutation, usePiholeOverview } from '@/api/pihole'

import { PiholeBlockingWidget } from './PiholeBlockingWidget'

vi.mock('@/api/pihole')

type Overview = Schema<'PiholeOverviewResponse'>
type BlockingResponse = Schema<'BlockingResponse'>
type BlockingRequest = Schema<'BlockingRequest'>

const BASE: Overview = {
  blocking_enabled: true,
  blocking_timer_seconds: null,
  gravity_domains: 1000,
  messages_count: 0,
  percent_blocked: 42,
  privacy_level: 0,
  query_frequency: 7,
  query_logging_enabled: true,
  up: true,
  updates_available: [],
  versions: [],
  query_feed_streaming: false,
}

function overview(overrides: Partial<Overview> = {}): Overview {
  return { ...BASE, ...overrides }
}

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

function err(status: number): UseQueryResult<Overview, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<Overview, ApiError>
}

function pending(): UseQueryResult<Overview, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<Overview, ApiError>
}

function mutationMock<V, R>(
  over: Partial<UseMutationResult<R, ApiError, V>> = {},
): UseMutationResult<R, ApiError, V> {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    isIdle: true,
    error: null,
    data: undefined,
    reset: vi.fn(),
    ...over,
  } as unknown as UseMutationResult<R, ApiError, V>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeBlockingWidget', () => {
  it('shows Loading… while pending', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(pending())
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the yellow temporarily-unavailable banner on 502', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(502))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText('Pi-hole metrics temporarily unavailable')).toBeInTheDocument()
  })

  it('renders ErrorDisplay on a non-502 error', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(500))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText(/Internal error/)).toBeInTheDocument()
  })

  it('shows "Blocking is on" and disable button when blocking_enabled is true', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText('Blocking is on.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Disable blocking/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Enable blocking/ })).not.toBeInTheDocument()
  })

  it('shows "Blocking is off" and enable button when blocking_enabled is false', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: false })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText(/Blocking is off/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Enable blocking/ })).toBeInTheDocument()
  })

  it('shows countdown when blocking is off with timer active', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(
      ok(overview({ blocking_enabled: false, blocking_timer_seconds: 300 })),
    )
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText(/re-enables in/)).toBeInTheDocument()
  })

  it('does not show countdown when timer is null or zero', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(
      ok(overview({ blocking_enabled: false, blocking_timer_seconds: null })),
    )
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.queryByText(/re-enables in/)).not.toBeInTheDocument()
  })

  it('shows "Blocking state unknown" when blocking_enabled is null', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: null })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)
    expect(screen.getByText(/Blocking state unknown/)).toBeInTheDocument()
    const enableButton = screen.getByRole('button', { name: /Enable blocking/ })
    expect(enableButton).toBeDisabled()
  })

  it('opens disable dialog and sends request with indefinite timer by default', () => {
    const mutate = vi.fn()
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeBlockingWidget />)

    const disableButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(disableButton)

    expect(screen.getByText('Disable Pi-hole blocking')).toBeInTheDocument()

    const input = screen.getByPlaceholderText('disable')
    fireEvent.change(input, { target: { value: 'disable' } })

    const confirmButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(confirmButton)

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        action: 'disable',
        confirm_phrase: 'disable',
      }),
      expect.anything(),
    )
    const callArgs = mutate.mock.calls[0]
    if (callArgs) {
      expect(callArgs[0]).not.toHaveProperty('timer')
    }
  })

  it('includes timer in request when disabling with a timed preset', () => {
    const mutate = vi.fn()
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeBlockingWidget />)

    const select = screen.getByTestId('pihole-disable-timer')
    fireEvent.change(select, { target: { value: '300' } })

    const disableButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(disableButton)

    const input = screen.getByPlaceholderText('disable')
    fireEvent.change(input, { target: { value: 'disable' } })

    const confirmButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(confirmButton)

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        action: 'disable',
        confirm_phrase: 'disable',
        timer: 300,
      }),
      expect.anything(),
    )
  })

  it('opens enable dialog and sends request without timer', () => {
    const mutate = vi.fn()
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: false })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeBlockingWidget />)

    const enableButton = screen.getByRole('button', { name: /^Enable blocking$/ })
    fireEvent.click(enableButton)

    expect(screen.getByText('Enable Pi-hole blocking')).toBeInTheDocument()

    const input = screen.getByPlaceholderText('enable')
    fireEvent.change(input, { target: { value: 'enable' } })

    const confirmButton = screen.getByRole('button', { name: /^Enable blocking$/ })
    fireEvent.click(confirmButton)

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        action: 'enable',
        confirm_phrase: 'enable',
      }),
      expect.anything(),
    )
    const callArgs = mutate.mock.calls[0]
    if (callArgs) {
      expect(callArgs[0]).not.toHaveProperty('timer')
    }
  })

  it('shows 400 error message in dialog', () => {
    const mutate = vi.fn(
      (
        vars: BlockingRequest,
        opts?: MutateOptions<BlockingResponse, ApiError, BlockingRequest>,
      ) => {
        opts?.onError?.(
          new RealApiError({
            status: 400,
            code: 'invalid_phrase',
            message: 'Invalid confirm phrase',
            retryAfterSeconds: null,
            details: null,
          }),
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeBlockingWidget />)

    const disableButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(disableButton)

    const input = screen.getByPlaceholderText('disable')
    fireEvent.change(input, { target: { value: 'disable' } })

    const confirmButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(confirmButton)

    expect(screen.getByText('Confirm phrase must be "disable"')).toBeInTheDocument()
  })

  it('shows pending state in dialog during mutation', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ isPending: true }))
    render(<PiholeBlockingWidget />)

    const disableButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(disableButton)

    expect(screen.getByRole('button', { name: /^Working…$/ })).toBeInTheDocument()
    const confirmButton = screen.getByRole('button', { name: /^Working…$/ })
    expect(confirmButton).toBeDisabled()
  })

  it('closes dialog on success', () => {
    const mutate = vi.fn(
      (
        vars: BlockingRequest,
        opts?: MutateOptions<BlockingResponse, ApiError, BlockingRequest>,
      ) => {
        opts?.onSuccess?.(
          { audit_id: 'a', blocking: 'enabled', timer: null },
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeBlockingWidget />)

    const disableButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(disableButton)

    expect(screen.getByText('Disable Pi-hole blocking')).toBeInTheDocument()

    const input = screen.getByPlaceholderText('disable')
    fireEvent.change(input, { target: { value: 'disable' } })

    const confirmButton = screen.getByRole('button', { name: /^Disable blocking$/ })
    fireEvent.click(confirmButton)

    expect(screen.queryByText('Disable Pi-hole blocking')).not.toBeInTheDocument()
  })

  it('shows timer select when blocking is enabled', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: true })))
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    render(<PiholeBlockingWidget />)

    const select = screen.getByTestId('pihole-disable-timer')
    expect(select).toBeInTheDocument()
    expect(select).toHaveValue('indefinite')
  })
})
