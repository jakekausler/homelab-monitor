import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { MutateOptions, UseMutationResult, UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { ApiError as RealApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useAdlists, useGravityUpdateMutation } from '@/api/pihole'

import { PiholeGravityWidget } from './PiholeGravityWidget'

vi.mock('@/api/pihole')
vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}))

import { toast } from 'sonner'

type Adlists = Schema<'PiholeAdlistsResponse'>
type GravityUpdateRequest = Schema<'GravityUpdateRequest'>
type GravityUpdateResponse = Schema<'GravityUpdateResponse'>

const BASE: Adlists = {
  gravity_domains: 1000,
  gravity_last_update_age_seconds: 3600,
  rows: [],
}

function adlists(overrides: Partial<Adlists> = {}): Adlists {
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

function err(status: number): UseQueryResult<Adlists, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<Adlists, ApiError>
}

function pending(): UseQueryResult<Adlists, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<Adlists, ApiError>
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

describe('PiholeGravityWidget', () => {
  it('shows Loading… while pending', () => {
    vi.mocked(useAdlists).mockReturnValue(pending())
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the yellow temporarily-unavailable banner on 502', () => {
    vi.mocked(useAdlists).mockReturnValue(err(502))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByText('Pi-hole adlists temporarily unavailable')).toBeInTheDocument()
  })

  it('renders ErrorDisplay on a non-502 error', () => {
    vi.mocked(useAdlists).mockReturnValue(err(500))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByText(/Internal error/)).toBeInTheDocument()
  })

  it('displays gravity domains and last update age', () => {
    vi.mocked(useAdlists).mockReturnValue(
      ok(adlists({ gravity_domains: 5000, gravity_last_update_age_seconds: 3600 })),
    )
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByText(/5,000/)).toBeInTheDocument()
    expect(screen.getByText(/1h ago/)).toBeInTheDocument()
  })

  it('shows "—" for null gravity_domains and gravity_last_update_age_seconds', () => {
    vi.mocked(useAdlists).mockReturnValue(
      ok(adlists({ gravity_domains: null, gravity_last_update_age_seconds: null })),
    )
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    const emDashes = screen.getAllByText('—')
    expect(emDashes.length).toBeGreaterThan(0)
  })

  it('shows EmptyState when no adlists are configured', () => {
    vi.mocked(useAdlists).mockReturnValue(ok(adlists({ rows: [] })))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByTestId('pihole-adlists-empty')).toBeInTheDocument()
    expect(screen.getByText('No adlists configured')).toBeInTheDocument()
  })

  it('renders adlist table with all rows', () => {
    const rows = [
      { list: 'List 1', address: 'https://list1.com', status: 'ok', enabled: true, domains: 1000 },
      {
        list: 'List 2',
        address: 'https://list2.com',
        status: 'download failed',
        enabled: false,
        domains: 500,
      },
      { list: 'List 3', address: 'https://list3.com', status: '', enabled: true, domains: null },
    ] as Schema<'PiholeAdlistRow'>[]
    vi.mocked(useAdlists).mockReturnValue(ok(adlists({ rows })))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)

    expect(screen.getByText('List 1')).toBeInTheDocument()
    expect(screen.getByText('List 2')).toBeInTheDocument()
    expect(screen.getByText('List 3')).toBeInTheDocument()
    expect(screen.getByText('https://list1.com')).toBeInTheDocument()
  })

  it('maps status to Badge variants correctly', () => {
    const rows = [
      { list: 'OK List', address: 'https://ok.com', status: 'ok', enabled: true, domains: 100 },
      {
        list: 'Failing List',
        address: 'https://fail.com',
        status: 'download failed',
        enabled: true,
        domains: 100,
      },
    ] as Schema<'PiholeAdlistRow'>[]
    vi.mocked(useAdlists).mockReturnValue(ok(adlists({ rows })))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)

    expect(screen.getByText('ok')).toBeInTheDocument()
    expect(screen.getByText('download failed')).toBeInTheDocument()
  })

  it('applies red tint to failing adlist rows', () => {
    const rows = [
      {
        list: 'Failing List',
        address: 'https://fail.com',
        status: 'download failed',
        enabled: true,
        domains: 100,
      },
    ] as Schema<'PiholeAdlistRow'>[]
    vi.mocked(useAdlists).mockReturnValue(ok(adlists({ rows })))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)

    const tr = screen.getByText('Failing List').closest('tr')
    expect(tr).toHaveClass('bg-red-500/5')
  })

  it('renders "Update gravity now" button', () => {
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)
    expect(screen.getByTestId('pihole-update-gravity-button')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Update gravity now/ })).toBeInTheDocument()
  })

  it('shows "Updating gravity…" and disables button during mutation', () => {
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ isPending: true }))
    render(<PiholeGravityWidget />)
    const button = screen.getByTestId('pihole-update-gravity-button')
    expect(button).toHaveTextContent('Updating gravity…')
    expect(button).toBeDisabled()
  })

  it('opens confirm dialog when clicking "Update gravity now"', () => {
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    expect(screen.getByText('Update Pi-hole gravity')).toBeInTheDocument()
  })

  it('sends mutation with confirm_phrase "update"', () => {
    const mutate = vi.fn()
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        confirm_phrase: 'update',
      }),
      expect.anything(),
    )
  })

  it('shows result dialog with log_tail on success', () => {
    const mutate = vi.fn(
      (
        vars: GravityUpdateRequest,
        opts?: MutateOptions<GravityUpdateResponse, ApiError, GravityUpdateRequest>,
      ) => {
        opts?.onSuccess?.(
          { audit_id: 'a', success: true, log_tail: ['line1', 'line2'] },
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(screen.getByTestId('pihole-gravity-log-tail')).toBeInTheDocument()
    expect(screen.getByText(/line1/)).toBeInTheDocument()
    expect(screen.getByText(/line2/)).toBeInTheDocument()
  })

  it('closes confirm dialog and opens result dialog on success', () => {
    const mutate = vi.fn(
      (
        vars: GravityUpdateRequest,
        opts?: MutateOptions<GravityUpdateResponse, ApiError, GravityUpdateRequest>,
      ) => {
        opts?.onSuccess?.(
          { audit_id: 'a', success: true, log_tail: ['output'] },
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(screen.queryByText('Update Pi-hole gravity')).not.toBeInTheDocument()
    expect(screen.getByText('Gravity update complete')).toBeInTheDocument()
  })

  it('shows failure title and toast error when success is false', () => {
    const mutate = vi.fn(
      (
        vars: GravityUpdateRequest,
        opts?: MutateOptions<GravityUpdateResponse, ApiError, GravityUpdateRequest>,
      ) => {
        opts?.onSuccess?.(
          { audit_id: 'a', success: false, log_tail: ['error output'] },
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(screen.queryByText('Update Pi-hole gravity')).not.toBeInTheDocument()
    expect(screen.getByText('Gravity update failed')).toBeInTheDocument()
    expect(screen.getByTestId('pihole-gravity-log-tail')).toBeInTheDocument()
    expect(screen.getByText(/error output/)).toBeInTheDocument()
    expect(vi.mocked(toast.error)).toHaveBeenCalledWith(
      'Gravity update reported failure — see log output',
    )
  })

  it('shows 400 error message in confirm dialog without toast', () => {
    const mutate = vi.fn(
      (
        vars: GravityUpdateRequest,
        opts?: MutateOptions<GravityUpdateResponse, ApiError, GravityUpdateRequest>,
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
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(screen.getByText('Confirm phrase must be "update"')).toBeInTheDocument()
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled()
  })

  it('shows non-400 error message in dialog and toasts error', () => {
    const mutate = vi.fn(
      (
        vars: GravityUpdateRequest,
        opts?: MutateOptions<GravityUpdateResponse, ApiError, GravityUpdateRequest>,
      ) => {
        opts?.onError?.(
          new RealApiError({
            status: 500,
            code: 'internal_error',
            message: 'Server error',
            retryAfterSeconds: null,
            details: null,
          }),
          vars,
          undefined,
          undefined as never,
        )
      },
    )
    vi.mocked(useAdlists).mockReturnValue(ok(adlists()))
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock({ mutate }))
    render(<PiholeGravityWidget />)

    const button = screen.getByTestId('pihole-update-gravity-button')
    fireEvent.click(button)

    const input = screen.getByPlaceholderText('update')
    fireEvent.change(input, { target: { value: 'update' } })

    const confirmButton = screen.getByRole('button', { name: /^Update gravity$/ })
    fireEvent.click(confirmButton)

    expect(screen.getByText(/Server error/)).toBeInTheDocument()
    expect(vi.mocked(toast.error)).toHaveBeenCalled()
  })
})
