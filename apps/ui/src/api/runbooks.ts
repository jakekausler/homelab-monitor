import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type Runbook = Schema<'RunbookOut'>
export type Approval = Schema<'ApprovalOut'>
export type ApprovalPlan = Schema<'PlanResponse'>
export type ApproveResponse = Schema<'ApproveResponse'>
export type RejectResponse = Schema<'RejectResponse'>
export type RefreshResponse = Schema<'RefreshResponse'>
export type RunbookGatesPatch = Schema<'RunbookGatesPatch'>

export const runbooksKeys = {
  all: ['runbooks'] as const,
} as const

export const approvalsKeys = {
  all: ['autofix-approvals'] as const,
  pending: ['autofix-approvals', 'pending'] as const,
  plan: (id: string) => ['autofix-approvals', 'plan', id] as const,
} as const

export type RunbookStats = Schema<'RunbookStatsOut'>

export const runbookStatsKeys = {
  all: ['runbook-stats'] as const,
} as const

/** GET /api/runbooks */
export function useRunbooks(): UseQueryResult<{ items: Runbook[] }, ApiError> {
  return useQuery({
    queryKey: runbooksKeys.all,
    queryFn: async () => {
      const result = await apiClient.GET('/api/runbooks', {})
      return unwrap<{ items: Runbook[] }>(result)
    },
    retry: false,
  })
}

/** GET /api/runbooks/stats */
export function useRunbookStats(): UseQueryResult<{ items: RunbookStats[] }, ApiError> {
  return useQuery({
    queryKey: runbookStatsKeys.all,
    queryFn: async () => {
      const result = await apiClient.GET('/api/runbooks/stats', {})
      return unwrap<{ items: RunbookStats[] }>(result)
    },
    retry: false,
  })
}

/** PATCH /api/runbooks/{runbook_id} */
export function useToggleRunbook(): UseMutationResult<
  Runbook,
  ApiError,
  { id: string; enabled?: boolean; auto_trigger?: boolean }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, ...body }) => {
      const result = await apiClient.PATCH('/api/runbooks/{runbook_id}', {
        params: { path: { runbook_id: id } },
        body,
      })
      return unwrap<Runbook>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: runbooksKeys.all })
    },
  })
}

/** POST /api/runbooks/refresh */
export function useRefreshRunbooks(): UseMutationResult<RefreshResponse, ApiError, void> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const result = await apiClient.POST('/api/runbooks/refresh', {})
      return unwrap<RefreshResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: runbooksKeys.all })
    },
  })
}

/** GET /api/autofix/approvals?status_filter=pending */
export function usePendingApprovals(): UseQueryResult<{ items: Approval[] }, ApiError> {
  return useQuery({
    queryKey: approvalsKeys.pending,
    queryFn: async () => {
      const result = await apiClient.GET('/api/autofix/approvals', {
        params: { query: { status_filter: 'pending' } },
      })
      return unwrap<{ items: Approval[] }>(result)
    },
    retry: false,
  })
}

/** GET /api/autofix/approvals/{approval_id}/plan */
export function useApprovalPlan(approvalId: string | null): UseQueryResult<ApprovalPlan, ApiError> {
  return useQuery({
    queryKey:
      approvalId !== null
        ? approvalsKeys.plan(approvalId)
        : ['autofix-approvals', 'plan', '__disabled__'],
    queryFn: async () => {
      if (approvalId === null) throw new Error('approvalId required')
      const result = await apiClient.GET('/api/autofix/approvals/{approval_id}/plan', {
        params: { path: { approval_id: approvalId } },
      })
      return unwrap<ApprovalPlan>(result)
    },
    enabled: approvalId !== null,
    retry: false,
  })
}

/** POST /api/autofix/approvals/{approval_id}/approve */
export function useApproveApproval(): UseMutationResult<
  ApproveResponse,
  ApiError,
  { approvalId: string; confirm_phrase?: string; confirm_pin?: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ approvalId, confirm_phrase, confirm_pin }) => {
      const result = await apiClient.POST('/api/autofix/approvals/{approval_id}/approve', {
        params: { path: { approval_id: approvalId } },
        body: { confirm_phrase: confirm_phrase ?? null, confirm_pin: confirm_pin ?? null },
      })
      return unwrap<ApproveResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: approvalsKeys.all })
    },
  })
}

/** POST /api/autofix/approvals/{approval_id}/reject */
export function useRejectApproval(): UseMutationResult<
  RejectResponse,
  ApiError,
  { approvalId: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ approvalId }) => {
      const result = await apiClient.POST('/api/autofix/approvals/{approval_id}/reject', {
        params: { path: { approval_id: approvalId } },
        body: {},
      })
      return unwrap<RejectResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: approvalsKeys.all })
    },
  })
}

export type TriggerResponse = Schema<'TriggerResponse'>

/** POST /api/runbooks/{runbook_id}/trigger */
export function useTriggerRunbook(): UseMutationResult<
  TriggerResponse,
  ApiError,
  { id: string; mode: 'dry_run' | 'real'; confirm_phrase?: string; confirm_pin?: string }
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (variables: {
      id: string
      mode: 'dry_run' | 'real'
      confirm_phrase?: string
      confirm_pin?: string
    }) => {
      const result = await apiClient.POST('/api/runbooks/{runbook_id}/trigger', {
        params: { path: { runbook_id: variables.id } },
        body: {
          mode: variables.mode,
          confirm_phrase: variables.confirm_phrase ?? null,
          confirm_pin: variables.confirm_pin ?? null,
        },
      })
      return unwrap(result)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: runbooksKeys.all })
      void queryClient.invalidateQueries({ queryKey: approvalsKeys.pending })
    },
  })
}
