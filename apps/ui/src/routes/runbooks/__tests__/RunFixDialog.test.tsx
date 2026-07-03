import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

import { RunFixDialog } from '../RunFixDialog'
import type { Runbook, TriggerResponse } from '@/api/runbooks'
import { useTriggerRunbook } from '@/api/runbooks'
import { usePinStatus } from '@/api/security-pin'
import { ApiError } from '@/api/client'

vi.mock('@/api/runbooks', () => ({
  useTriggerRunbook: vi.fn(),
}))

vi.mock('@/api/security-pin', () => ({
  usePinStatus: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { toast } from 'sonner'

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

function mockPinStatus(overrides: Record<string, unknown> = {}) {
  return {
    data: { set: false },
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof usePinStatus>
}

const SAFE_RUNBOOK: Runbook = {
  id: 'runbook-safe',
  path: 'runbooks/safe-example',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [{ alertname: 'SafeAlert' }],
  risk_tag: 'safe',
  dry_run_required: false,
  rate_limit_per_hour: 5,
  cooldown_seconds: 300,
  enabled: true,
  auto_trigger: false,
  content_hash: 'hash-safe',
}

const RISKY_RUNBOOK: Runbook = {
  id: 'runbook-risky',
  path: 'runbooks/risky-example',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [{ alertname: 'RiskyAlert' }],
  risk_tag: 'risky',
  dry_run_required: true,
  rate_limit_per_hour: null,
  cooldown_seconds: null,
  enabled: false,
  auto_trigger: false,
  content_hash: 'hash-risky',
}

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children)
  }
}

beforeEach(() => {
  // Default: no PIN configured — phrase branch renders (regression baseline for
  // all pre-existing tests below that don't explicitly override this mock).
  vi.mocked(usePinStatus).mockReturnValue(mockPinStatus())
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RunFixDialog', () => {
  it('renders the dialog when open=true', () => {
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation())
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.getByTestId('run-fix-dialog')).toBeInTheDocument()
  })

  it('does not render dialog content when open=false', () => {
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation())
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={false} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.queryByTestId('run-fix-dialog')).not.toBeInTheDocument()
  })

  it('safe runbook: both mode radios visible, real is enabled', () => {
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation())
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.getByTestId('run-fix-mode-dry')).toBeInTheDocument()
    const realRadio = screen.getByTestId('run-fix-mode-real')
    expect(realRadio).toBeInTheDocument()
    expect(realRadio).not.toBeDisabled()
  })

  it('risky runbook: real mode radio disabled, inline explanation visible', () => {
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation())
    render(<RunFixDialog runbook={RISKY_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.getByTestId('run-fix-mode-real')).toBeDisabled()
    expect(screen.getByText(/risky runbook requires approval flow/i)).toBeInTheDocument()
  })

  it('submitting dry_run calls mutate with {id, mode: dry_run} and closes on success', () => {
    const mutate = vi.fn(
      (_vars: unknown, opts?: { onSuccess?: (data: TriggerResponse) => void }) => {
        opts?.onSuccess?.({
          run_id: 'run-1',
          outcome: 'dry_run_stored',
          denial_reason: null,
          approval_id: 'appr-1',
        })
      },
    )
    const onOpenChange = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={onOpenChange} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(mutate).toHaveBeenCalledWith({ id: 'runbook-safe', mode: 'dry_run' }, expect.any(Object))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('submitting real (safe runbook) opens ConfirmPhraseDialog with expectedPhrase = basename; does not call mutate yet', () => {
    const mutate = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))

    // ConfirmPhraseDialog renders with placeholder = expectedPhrase (basename of path)
    const phraseInput = screen.getByPlaceholderText('safe-example')
    expect(phraseInput).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()
  })

  it('confirming the phrase calls mutate with {id, mode: real, confirm_phrase: basename} (behavior change vs 010A)', () => {
    const mutate = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))

    const phraseInput = screen.getByPlaceholderText('safe-example')
    fireEvent.change(phraseInput, { target: { value: 'safe-example' } })

    const confirmButton = screen.getByRole('button', { name: /^Run for real$/ })
    fireEvent.click(confirmButton)

    expect(mutate).toHaveBeenCalledWith(
      { id: 'runbook-safe', mode: 'real', confirm_phrase: 'safe-example' },
      expect.any(Object),
    )
  })

  it('renders ConfirmPinDialog instead of ConfirmPhraseDialog for real mode when usePinStatus returns set:true', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation())
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(screen.queryByPlaceholderText('safe-example')).not.toBeInTheDocument()
    expect(screen.getByLabelText('PIN')).toBeInTheDocument()
  })

  it('PIN branch: onConfirm(pin) results in mutate called with {id, mode: real, confirm_pin} and no confirm_phrase key', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    const mutate = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))

    fireEvent.change(screen.getByLabelText('PIN'), { target: { value: '1234' } })
    fireEvent.click(screen.getByRole('button', { name: /^Run for real$/ }))

    expect(mutate).toHaveBeenCalledWith(
      { id: 'runbook-safe', mode: 'real', confirm_pin: '1234' },
      expect.any(Object),
    )
    const [body] = mutate.mock.calls[0] as [{ confirm_phrase?: string }]
    expect(body).not.toHaveProperty('confirm_phrase')
  })

  it('dry_run mode: no dialog renders and mutate body has neither confirm_phrase nor confirm_pin (regression)', () => {
    const mutate = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(screen.queryByPlaceholderText('safe-example')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('PIN')).not.toBeInTheDocument()
    expect(mutate).toHaveBeenCalledWith({ id: 'runbook-safe', mode: 'dry_run' }, expect.any(Object))
    const [body] = mutate.mock.calls[0] as [{ confirm_phrase?: string; confirm_pin?: string }]
    expect(body).not.toHaveProperty('confirm_phrase')
    expect(body).not.toHaveProperty('confirm_pin')
  })

  it('429/retry-after shaped trigger error passes retryAfterSeconds through to ConfirmPinDialog', () => {
    vi.mocked(usePinStatus).mockReturnValue(mockPinStatus({ data: { set: true } }))
    const err = new ApiError({
      status: 429,
      code: 'pin_locked',
      message: 'PIN entry locked. Try again in 30s.',
      retryAfterSeconds: 30,
      details: null,
    })
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ error: err }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent === 'Too many wrong attempts. Please wait 30s before trying again.',
      ).length,
    ).toBeGreaterThan(0)
  })

  it('success on real mode: toast.success called and dialog closes', () => {
    const mutate = vi.fn(
      (_vars: unknown, opts?: { onSuccess?: (data: TriggerResponse) => void }) => {
        opts?.onSuccess?.({
          run_id: 'run-2',
          outcome: 'ran',
          denial_reason: null,
          approval_id: null,
        })
      },
    )
    const onOpenChange = vi.fn()
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={onOpenChange} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-mode-real'))
    fireEvent.click(screen.getByTestId('run-fix-submit'))
    const phraseInput = screen.getByPlaceholderText('safe-example')
    fireEvent.change(phraseInput, { target: { value: 'safe-example' } })
    fireEvent.click(screen.getByRole('button', { name: /^Run for real$/ }))

    expect(toast.success).toHaveBeenCalled()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('denial toast renders human-readable text for kill_switch code', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onError?: (err: unknown) => void }) => {
      opts?.onError?.(
        new ApiError({
          status: 409,
          code: 'kill_switch',
          message: 'kill_switch',
          retryAfterSeconds: null,
          details: null,
        }),
      )
    })
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(toast.error).toHaveBeenCalledWith('Global kill-switch is engaged')
  })

  it('denial toast renders human-readable text for dry_run_required_for_risky code', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onError?: (err: unknown) => void }) => {
      opts?.onError?.(
        new ApiError({
          status: 400,
          code: 'dry_run_required_for_risky',
          message: 'dry_run_required_for_risky',
          retryAfterSeconds: null,
          details: null,
        }),
      )
    })
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(toast.error).toHaveBeenCalledWith('This runbook is risky and must be dry-run + approved')
  })

  it('unknown denial code falls back to err.message', () => {
    const mutate = vi.fn((_vars: unknown, opts?: { onError?: (err: unknown) => void }) => {
      opts?.onError?.(
        new ApiError({
          status: 500,
          code: 'something_unmapped',
          message: 'Something went wrong on the server',
          retryAfterSeconds: null,
          details: null,
        }),
      )
    })
    vi.mocked(useTriggerRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunFixDialog runbook={SAFE_RUNBOOK} open={true} onOpenChange={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('run-fix-submit'))

    expect(toast.error).toHaveBeenCalledWith('Something went wrong on the server')
  })
})
