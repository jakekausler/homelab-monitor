import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type LogUserRuleListResponse = Schema<'LogUserRuleListResponse'>
export type LogUserRuleResponse = Schema<'LogUserRuleResponse'>
export type LogUserRuleCreateRequest = Schema<'LogUserRuleCreateRequest'>
export type LogUserRulePatchRequest = Schema<'LogUserRulePatchRequest'>
export type LogUserRulesHealthResponse = Schema<'LogUserRulesHealthResponse'>

export const userRuleKeys = {
  all: ['user-rules'] as const,
  list: () => ['user-rules', 'list'] as const,
}

export function useUserRules(): UseQueryResult<LogUserRuleListResponse, ApiError> {
  return useQuery({
    queryKey: userRuleKeys.list(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/user-rules')
      return unwrap<LogUserRuleListResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

export function useCreateUserRule(): UseMutationResult<
  LogUserRuleResponse,
  ApiError,
  LogUserRuleCreateRequest
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body) => {
      const result = await apiClient.POST('/api/logs/user-rules', { body })
      return unwrap<LogUserRuleResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userRuleKeys.all })
    },
  })
}

const HEALTH_REFETCH_MS = 20_000

export function useUserRulesHealth(): UseQueryResult<LogUserRulesHealthResponse, ApiError> {
  return useQuery({
    queryKey: ['user-rules', 'health'] as const,
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/user-rules-health')
      return unwrap<LogUserRulesHealthResponse>(result)
    },
    refetchInterval: HEALTH_REFETCH_MS,
    staleTime: HEALTH_REFETCH_MS,
    retry: false,
  })
}

export function usePatchUserRule(): UseMutationResult<
  LogUserRuleResponse,
  ApiError,
  { rule_id: number; body: LogUserRulePatchRequest }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ rule_id, body }) => {
      const result = await apiClient.PATCH('/api/logs/user-rules/{rule_id}', {
        params: { path: { rule_id } },
        body,
      })
      return unwrap<LogUserRuleResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userRuleKeys.all })
    },
  })
}

export function useDeleteUserRule(): UseMutationResult<void, ApiError, number> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rule_id) => {
      const result = await apiClient.DELETE('/api/logs/user-rules/{rule_id}', {
        params: { path: { rule_id } },
      })
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userRuleKeys.all })
    },
  })
}

export function useEnableUserRule(): UseMutationResult<LogUserRuleResponse, ApiError, number> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rule_id) => {
      const result = await apiClient.POST('/api/logs/user-rules/{rule_id}/enable', {
        params: { path: { rule_id } },
      })
      return unwrap<LogUserRuleResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userRuleKeys.all })
    },
  })
}

export function useDisableUserRule(): UseMutationResult<LogUserRuleResponse, ApiError, number> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (rule_id) => {
      const result = await apiClient.POST('/api/logs/user-rules/{rule_id}/disable', {
        params: { path: { rule_id } },
      })
      return unwrap<LogUserRuleResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: userRuleKeys.all })
    },
  })
}
