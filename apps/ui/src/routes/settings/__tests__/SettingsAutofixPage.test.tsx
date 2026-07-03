// Project test conventions:
// - Vitest (explicit imports), vi.mock at top, QueryClientProvider wrapper
// - Pattern mirrors SettingsLogsPage.test.tsx: mock the @/api/* hooks module directly
// - ConfirmPhraseDialog requires typing expectedPhrase into #confirm-phrase-input before
//   the confirm button (by confirmLabel text) is enabled.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { KillSwitchState, KillSwitchToggleResponse } from '@/api/autofixSettings'
import { ApiError } from '@/api/client'

vi.mock('@/api/autofixSettings', () => ({
  useAutofixKillSwitch: vi.fn(),
  useToggleAutofixKillSwitch: vi.fn(),
}))

vi.mock('@/api/security-pin', () => ({
  usePinStatus: vi.fn(),
}))

import { useAutofixKillSwitch, useToggleAutofixKillSwitch } from '@/api/autofixSettings'
import { usePinStatus } from '@/api/security-pin'
import { SettingsAutofixPage } from '@/routes/settings/SettingsAutofixPage'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const ENABLED_STATE: KillSwitchState = {
  enabled: true,
  updated_at: '2026-01-01T00:00:00Z',
}

const DISABLED_STATE: KillSwitchState = {
  enabled: false,
  updated_at: null,
}

function mockQuery(
  overrides: Partial<ReturnType<typeof useAutofixKillSwitch>> = {},
): ReturnType<typeof useAutofixKillSwitch> {
  return {
    data: ENABLED_STATE,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof useAutofixKillSwitch>
}

function mockMutation(
  overrides: Partial<ReturnType<typeof useToggleAutofixKillSwitch>> = {},
): ReturnType<typeof useToggleAutofixKillSwitch> {
  return {
    mutate: vi.fn(),
    isPending: false,
    error: null,
    data: undefined,
    ...overrides,
  } as unknown as ReturnType<typeof useToggleAutofixKillSwitch>
}

function mockPinStatus(overrides: Record<string, unknown> = {}) {
  return {
    data: { set: false },
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof usePinStatus>
}

beforeEach(() => {
  // Default: no PIN configured — phrase branch renders (regression baseline).
  vi.mocked(usePinStatus).mockReturnValue(mockPinStatus())
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SettingsAutofixPage', () => {
  it('renders current enabled state', () => {
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('autofix-status').textContent).toBe('Enabled')
    expect(screen.getByTestId('autofix-toggle').textContent).toBe('Disable auto-fix')
  })

  it('renders current disabled state', () => {
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: DISABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('autofix-status').textContent).toBe('Disabled')
    expect(screen.getByTestId('autofix-toggle').textContent).toBe('Enable auto-fix')
  })

  it('opens ConfirmPhraseDialog with expected phrase "disable auto-fix" when currently enabled', () => {
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))

    expect(screen.getByText('disable auto-fix')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('disable auto-fix')).toBeInTheDocument()
  })

  it('opens ConfirmPhraseDialog with expected phrase "enable auto-fix" when currently disabled', () => {
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: DISABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))

    expect(screen.getByText('enable auto-fix')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('enable auto-fix')).toBeInTheDocument()
  })

  it('fires mutation with correct body on confirm', () => {
    const mutate = vi.fn()
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ mutate }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))
    fireEvent.change(screen.getByPlaceholderText('disable auto-fix'), {
      target: { value: 'disable auto-fix' },
    })
    fireEvent.click(screen.getByText('I understand — proceed'))

    expect(mutate).toHaveBeenCalledWith(
      { enabled: false, confirm_phrase: 'disable auto-fix' },
      expect.objectContaining({ onSuccess: expect.any(Function) as unknown }),
    )
  })

  it('renders killed_inflight_run_id banner on success', () => {
    const response: KillSwitchToggleResponse = {
      enabled: false,
      updated_at: '2026-01-02T00:00:00Z',
      killed_inflight_run_id: 'abc-123',
      unwind_warning: null,
    }
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ data: response }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('autofix-killed-run').textContent).toContain(
      'Killed in-flight run abc-123',
    )
  })

  it('renders unwind_warning banner on success', () => {
    const response: KillSwitchToggleResponse = {
      enabled: false,
      updated_at: '2026-01-02T00:00:00Z',
      killed_inflight_run_id: null,
      unwind_warning: 'unwind_deadline_exceeded',
    }
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ data: response }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('autofix-unwind-warning').textContent).toContain(
      'unwind_deadline_exceeded',
    )
  })

  it('renders error message from ApiError on 502', () => {
    const apiError = new ApiError({
      status: 502,
      code: 'bad_gateway',
      message: 'Upstream service unavailable.',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ error: apiError }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))

    expect(screen.getByRole('alert').textContent).toBe('Upstream service unavailable.')
  })

  it('case-fold phrase semantics regression: lowercase phrase entry still enables confirm (preserved from 007)', () => {
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))
    fireEvent.change(screen.getByPlaceholderText('disable auto-fix'), {
      target: { value: '  DISABLE AUTO-FIX  ' },
    })

    expect(screen.getByText('I understand — proceed')).not.toBeDisabled()
  })

  it('PIN branch: renders ConfirmPinDialog instead of ConfirmPhraseDialog when usePinStatus returns set:true', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation())
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))

    expect(screen.queryByPlaceholderText('disable auto-fix')).not.toBeInTheDocument()
    expect(screen.getByLabelText('PIN')).toBeInTheDocument()
  })

  it('PIN branch: kill-switch mutation body carries confirm_pin (no confirm_phrase)', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    const mutate = vi.fn()
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ mutate }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))
    fireEvent.change(screen.getByLabelText('PIN'), { target: { value: '1234' } })
    const dialog = screen.getByRole('dialog', { name: /Disable auto-fix/i })
    fireEvent.click(within(dialog).getByRole('button', { name: /I understand — proceed/i }))

    expect(mutate).toHaveBeenCalledWith({ enabled: false, confirm_pin: '1234' }, expect.any(Object))
    const [body] = mutate.mock.calls[0] as [{ confirm_phrase?: string }]
    expect(body).not.toHaveProperty('confirm_phrase')
  })

  it('429 countdown from toggle mutation error is passed through to ConfirmPinDialog', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    const err = new ApiError({
      status: 429,
      code: 'pin_locked',
      message: 'PIN entry locked. Try again in 30s.',
      retryAfterSeconds: 30,
      details: null,
    })
    vi.mocked(useAutofixKillSwitch).mockReturnValue(mockQuery({ data: ENABLED_STATE }))
    vi.mocked(useToggleAutofixKillSwitch).mockReturnValue(mockMutation({ error: err }))
    render(<SettingsAutofixPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('autofix-toggle'))

    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent === 'Too many wrong attempts. Please wait 30s before trying again.',
      ).length,
    ).toBeGreaterThan(0)
  })
})
