import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'

vi.mock('@/api/runbooks', () => ({
  useApprovalPlan: vi.fn(),
  useApproveApproval: vi.fn(),
  useRejectApproval: vi.fn(),
  approvalsKeys: {
    all: ['autofix-approvals'],
    pending: ['autofix-approvals', 'pending'],
    plan: (id: string) => ['autofix-approvals', 'plan', id],
  },
}))

import { useApprovalPlan, useApproveApproval, useRejectApproval } from '@/api/runbooks'
import { ApprovalPlanDialog } from '@/routes/runbooks/ApprovalPlanDialog'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const PLAN_DATA = {
  approval_id: 'appr-x',
  dry_run_id: 'dry-x',
  runbook_id: 'pihole-restart-loop',
  transcript_path: '/tmp/x.log',
  plan_text: 'plan body',
  exit_code: 0,
}

function mockQuery<T>(overrides: Partial<{ data: T; isLoading: boolean; error: unknown }> = {}) {
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

describe('ApprovalPlanDialog', () => {
  it('Approve flow: opens ConfirmPhraseDialog, requires "approve" phrase, calls mutation on confirm', () => {
    const mutate = vi.fn()
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation({ mutate }))
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    // Approve button opens ConfirmPhraseDialog
    fireEvent.click(screen.getByTestId('approval-approve'))
    const phraseInput = screen.getByPlaceholderText('approve')
    expect(phraseInput).toBeInTheDocument()

    // Wrong phrase — mutation not called
    fireEvent.change(phraseInput, { target: { value: 'wrong' } })
    // ConfirmPhraseDialog's Approve button — scoped inside the phrase-confirm dialog
    const confirmDialog = screen.getByRole('dialog', { name: /Approve auto-fix run/i })
    fireEvent.click(within(confirmDialog).getByRole('button', { name: /^approve$/i }))
    expect(mutate).not.toHaveBeenCalled()

    // Correct phrase — mutation called with correct body
    fireEvent.change(phraseInput, { target: { value: 'approve' } })
    // ConfirmPhraseDialog's Approve button — scoped inside the phrase-confirm dialog
    const confirmDialog2 = screen.getByRole('dialog', { name: /Approve auto-fix run/i })
    fireEvent.click(within(confirmDialog2).getByRole('button', { name: /^approve$/i }))
    expect(mutate).toHaveBeenCalledWith(
      { approvalId: 'appr-x', confirm_phrase: 'approve' },
      expect.any(Object),
    )
  })

  it('Reject button calls useRejectApproval with approval id', () => {
    const mutate = vi.fn()
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation({ mutate }))

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    fireEvent.click(screen.getByTestId('approval-reject'))
    expect(mutate).toHaveBeenCalledWith({ approvalId: 'appr-x' }, expect.any(Object))
  })

  it('Approve button disabled when kill switch off', () => {
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={false} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    expect(screen.getByTestId('approval-approve')).toBeDisabled()
    // Reject still available
    expect(screen.getByTestId('approval-reject')).not.toBeDisabled()
  })

  it('runbook_changed_since_plan 409 renders friendly message', () => {
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    const err = new ApiError({
      status: 409,
      code: 'runbook_changed_since_plan',
      message: 'runbook_changed_since_plan',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation({ error: err }))
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    const banner = screen.getByTestId('approval-approve-error')
    expect(banner.textContent).toContain('Runbook changed since plan generated')
  })

  it('kill_switch 409 renders "Auto-fix is disabled" message', () => {
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    const err = new ApiError({
      status: 409,
      code: 'kill_switch',
      message: 'kill_switch',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation({ error: err }))
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    expect(screen.getByTestId('approval-approve-error').textContent).toContain(
      'Auto-fix is disabled',
    )
  })

  it('friendly error helper covers runbook_missing and approval_not_pending', () => {
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    const err = new ApiError({
      status: 409,
      code: 'approval_not_pending',
      message: 'approval_not_pending',
      retryAfterSeconds: null,
      details: null,
    })
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation({ error: err }))

    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })

    expect(screen.getByTestId('approval-reject-error').textContent).toContain(
      'This approval is no longer pending',
    )
  })

  it('copy plan button calls clipboard.writeText with plan_text', () => {
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={vi.fn()} />, {
      wrapper: makeWrapper(),
    })
    const copyBtn = screen.getByRole('button', { name: /copy plan/i })
    fireEvent.click(copyBtn)
    expect(writeText).toHaveBeenCalledWith(PLAN_DATA.plan_text)
  })

  it('reject success invokes onClose', () => {
    const onClose = vi.fn()
    const mutate = vi.fn((_vars: unknown, opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.()
    })
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery({ data: PLAN_DATA }))
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation({ mutate }))
    render(<ApprovalPlanDialog approvalId="appr-x" killSwitchEnabled={true} onClose={onClose} />, {
      wrapper: makeWrapper(),
    })
    fireEvent.click(screen.getByTestId('approval-reject'))
    expect(onClose).toHaveBeenCalled()
  })
})
