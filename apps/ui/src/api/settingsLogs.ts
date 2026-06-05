import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type LogsRetentionResponse = Schema<'LogsRetentionResponse'>
export type LogsRetentionUpdateRequest = Schema<'LogsRetentionUpdateRequest'>

export const settingsLogsKeys = {
  all: ['settings-logs'] as const,
  retention: () => ['settings-logs', 'retention'] as const,
}

export function useLogsRetention(): UseQueryResult<LogsRetentionResponse, ApiError> {
  return useQuery({
    queryKey: settingsLogsKeys.retention(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/settings/logs/retention', {})
      return unwrap<LogsRetentionResponse>(result)
    },
    retry: false,
  })
}

export function useUpdateLogsRetention(): UseMutationResult<
  LogsRetentionResponse,
  ApiError,
  LogsRetentionUpdateRequest
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: LogsRetentionUpdateRequest) => {
      const result = await apiClient.PATCH('/api/settings/logs/retention', { body })
      return unwrap<LogsRetentionResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: settingsLogsKeys.all })
    },
  })
}
