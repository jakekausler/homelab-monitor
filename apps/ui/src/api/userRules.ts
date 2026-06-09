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
