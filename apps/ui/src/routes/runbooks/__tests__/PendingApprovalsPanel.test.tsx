import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiError } from '@/api/client'

vi.mock('@/api/runbooks', () => ({
  usePendingApprovals: vi.fn(),
  useApprovalPlan: vi.fn(),
  useApproveApproval: vi.fn(),
  useRejectApproval: vi.fn(),
  approvalsKeys: {
    all: ['autofix-approvals'],
    pending: ['autofix-approvals', 'pending'],
    plan: (id: string) => ['autofix-approvals', 'plan', id],
  },
}))

import {
  usePendingApprovals,
  useApprovalPlan,
  useApproveApproval,
  useRejectApproval,
  type Approval,
} from '@/api/runbooks'
import { PendingApprovalsPanel } from '@/routes/runbooks/PendingApprovalsPanel'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const APPROVAL_A: Approval = {
  id: 'appr-a',
  dry_run_id: 'dry-a',
  runbook_id: 'pihole-restart-loop',
  alert_id: 'alert-1',
  pinned_runbook_hash: 'hash-a',
  status: 'pending',
  approved_by: null,
  decided_at: null,
  real_run_id: null,
  created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  drift_detected: false,
}

const APPROVAL_B: Approval = {
  id: 'appr-b',
  dry_run_id: 'dry-b',
  runbook_id: 'nas-reboot',
  alert_id: null,
  pinned_runbook_hash: 'hash-b',
  status: 'pending',
  approved_by: null,
  decided_at: null,
  real_run_id: null,
  created_at: new Date(Date.now() - 2 * 60 * 60_000).toISOString(),
  drift_detected: true,
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

describe('PendingApprovalsPanel', () => {
  it('renders one row per pending approval and drift badge only on drifted', () => {
    vi.mocked(usePendingApprovals).mockReturnValue(
      mockQuery({ data: { items: [APPROVAL_A, APPROVAL_B] } }),
    )
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery())
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<PendingApprovalsPanel killSwitchEnabled={true} />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('pending-approvals-row-appr-a')).toBeInTheDocument()
    expect(screen.getByTestId('pending-approvals-row-appr-b')).toBeInTheDocument()
    expect(screen.queryByTestId('pending-approvals-drift-appr-a')).not.toBeInTheDocument()
    expect(screen.getByTestId('pending-approvals-drift-appr-b')).toBeInTheDocument()
  })

  it('empty state renders when zero pending', () => {
    vi.mocked(usePendingApprovals).mockReturnValue(mockQuery({ data: { items: [] } }))
    vi.mocked(useApprovalPlan).mockReturnValue(mockQuery())
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<PendingApprovalsPanel killSwitchEnabled={true} />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('pending-approvals-empty')).toBeInTheDocument()
  })

  it('View button opens ApprovalPlanDialog', () => {
    vi.mocked(usePendingApprovals).mockReturnValue(mockQuery({ data: { items: [APPROVAL_A] } }))
    vi.mocked(useApprovalPlan).mockReturnValue(
      mockQuery({
        data: {
          approval_id: 'appr-a',
          dry_run_id: 'dry-a',
          runbook_id: 'pihole-restart-loop',
          transcript_path: '/tmp/transcript.log',
          plan_text: '=== PLAN ===\nrestart pi-hole',
          exit_code: 0,
        },
      }),
    )
    vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
    vi.mocked(useRejectApproval).mockReturnValue(mockMutation())

    render(<PendingApprovalsPanel killSwitchEnabled={true} />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('pending-approvals-view-appr-a'))

    // Dialog opens — plan text renders
    expect(screen.getByTestId('approval-plan-text')).toBeInTheDocument()
    expect(screen.getByTestId('approval-plan-text').textContent).toContain('restart pi-hole')
  })
})
