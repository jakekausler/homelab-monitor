// Project test conventions (mirrors SettingsAutofixPage.test.tsx / SettingsLogsPage.test.tsx):
// - vi.mock the @/api/* hooks module directly at top; import mocked hooks after
// - QueryClientProvider wrapper via makeWrapper()
// - mockQuery/mockMutation helpers returning overridable shapes
// - fireEvent (not userEvent) for interactions
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'

vi.mock('@/api/security-pin', () => ({
  usePinStatus: vi.fn(),
  useSetPin: vi.fn(),
  useDeletePin: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { usePinStatus, useSetPin, useDeletePin } from '@/api/security-pin'
import { toast } from 'sonner'
import { SettingsSecurityPage } from '@/routes/settings/SettingsSecurityPage'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

function mockQuery<T>(
  overrides: Partial<{ data: T; isLoading: boolean; error: unknown }> = {},
): UseQueryResult<T, ApiError> {
  return {
    data: undefined as unknown as T,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as UseQueryResult<T, ApiError>
}

function mockMutation<TData = unknown, TVariables = unknown>(
  overrides: Record<string, unknown> = {},
): UseMutationResult<TData, ApiError, TVariables> {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    error: null,
    data: undefined,
    reset: vi.fn(),
    ...overrides,
  } as unknown as UseMutationResult<TData, ApiError, TVariables>
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SettingsSecurityPage', () => {
  it('shows "No PIN configured" when usePinStatus returns { set: false }', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('pin-status').textContent).toBe('No PIN configured')
    expect(screen.getByTestId('set-pin-button')).toBeInTheDocument()
  })

  it('shows "PIN is set" and Rotate/Delete buttons when usePinStatus returns { set: true }', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('pin-status').textContent).toBe('PIN is set')
    expect(screen.getByTestId('rotate-pin-button')).toBeInTheDocument()
    expect(screen.getByTestId('delete-pin-button')).toBeInTheDocument()
  })

  it('opens Set PIN dialog on trigger click and closes on Cancel', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('set-pin-button'))
    expect(
      screen.getByText('Set a numeric PIN for confirming destructive actions'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
    expect(
      screen.queryByText('Set a numeric PIN for confirming destructive actions'),
    ).not.toBeInTheDocument()
  })

  it('Set PIN dialog: mismatched new_pin/confirm_new_pin shows inline error, does not call mutate', () => {
    const mutate = vi.fn()
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('set-pin-button'))
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm PIN'), { target: { value: '4321' } })
    fireEvent.change(screen.getByLabelText('Current password'), {
      target: { value: 'my-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Set PIN$/ }))

    expect(screen.getByRole('alert').textContent).toBe('PINs do not match')
    expect(mutate).not.toHaveBeenCalled()
  })

  it('Set PIN dialog: valid matching pins + password calls mutate with new_pin + current_password only', () => {
    const mutate = vi.fn()
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('set-pin-button'))
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm PIN'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Current password'), {
      target: { value: 'my-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Set PIN$/ }))

    expect(mutate).toHaveBeenCalledWith(
      { new_pin: '1234', current_password: 'my-password' },
      expect.any(Object),
    )
    const [, body] = mutate.mock.calls[0] as [unknown, { current_pin?: string }]
    expect(body).not.toHaveProperty('current_pin')
  })

  it('Rotate dialog: valid inputs call mutate with new_pin + current_pin only', () => {
    const mutate = vi.fn()
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('rotate-pin-button'))
    fireEvent.change(screen.getByLabelText('Current PIN'), { target: { value: '9999' } })
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm new PIN'), { target: { value: '1234' } })
    fireEvent.click(screen.getByRole('button', { name: /^Rotate PIN$/ }))

    expect(mutate).toHaveBeenCalledWith(
      { new_pin: '1234', current_pin: '9999' },
      expect.any(Object),
    )
    const [, body] = mutate.mock.calls[0] as [unknown, { current_password?: string }]
    expect(body).not.toHaveProperty('current_password')
  })

  it('Rotate dialog: 429-shaped error surfaces a countdown to the user', () => {
    const err = new ApiError({
      status: 429,
      code: 'pin_locked',
      message: 'PIN entry locked. Try again in 30s.',
      retryAfterSeconds: 30,
      details: null,
    })
    const mutate = vi.fn((_vars: unknown, opts?: { onError?: (e: unknown) => void }) => {
      opts?.onError?.(err)
    })
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('rotate-pin-button'))
    fireEvent.change(screen.getByLabelText('Current PIN'), { target: { value: '9999' } })
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm new PIN'), { target: { value: '1234' } })
    fireEvent.click(screen.getByRole('button', { name: /^Rotate PIN$/ }))

    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent === 'Too many wrong attempts. Please wait 30s before trying again.',
      ).length,
    ).toBeGreaterThan(0)
  })

  it('Delete dialog: valid current_password calls useDeletePin mutate with current_password', () => {
    const mutate = vi.fn()
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation({ mutate }))

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('delete-pin-button'))
    fireEvent.change(screen.getByLabelText('Current password'), {
      target: { value: 'my-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Delete PIN$/ }))

    expect(mutate).toHaveBeenCalledWith({ current_password: 'my-password' }, expect.any(Object))
  })

  it('shows success toast when Set PIN mutation succeeds', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.()
    })
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('set-pin-button'))
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm PIN'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Current password'), {
      target: { value: 'my-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Set PIN$/ }))

    expect(toast.success).toHaveBeenCalledWith('PIN set successfully')
  })

  it('shows success toast when Rotate PIN mutation succeeds', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.()
    })
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('rotate-pin-button'))
    fireEvent.change(screen.getByLabelText('Current PIN'), { target: { value: '9999' } })
    fireEvent.change(screen.getByLabelText('New PIN (4-12 digits)'), { target: { value: '1234' } })
    fireEvent.change(screen.getByLabelText('Confirm new PIN'), { target: { value: '1234' } })
    fireEvent.click(screen.getByRole('button', { name: /^Rotate PIN$/ }))

    expect(toast.success).toHaveBeenCalledWith('PIN rotated successfully')
  })

  it('shows success toast when Delete PIN mutation succeeds', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.()
    })
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation({ mutate }))

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('delete-pin-button'))
    fireEvent.change(screen.getByLabelText('Current password'), {
      target: { value: 'my-password' },
    })
    fireEvent.click(screen.getByRole('button', { name: /^Delete PIN$/ }))

    expect(toast.success).toHaveBeenCalledWith('PIN removed')
  })

  it('Set PIN submit button disabled and shows spinner label while isPending', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: false } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation({ isPending: true }))
    vi.mocked(useDeletePin).mockReturnValue(mockMutation())

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('set-pin-button')).toBeDisabled()
  })

  it('Delete PIN submit button disabled and shows spinner label while isPending', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockQuery({ data: { set: true } }))
    vi.mocked(useSetPin).mockReturnValue(mockMutation())
    vi.mocked(useDeletePin).mockReturnValue(mockMutation({ isPending: true }))

    render(<SettingsSecurityPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('delete-pin-button')).toBeDisabled()
  })
})
