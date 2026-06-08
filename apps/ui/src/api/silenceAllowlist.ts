import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type SilenceAllowlistListResponse = Schema<'SilenceAllowlistListResponse'>
export type SilenceAllowlistResponse = Schema<'SilenceAllowlistResponse'>
export type SilenceAllowlistCreateRequest = Schema<'SilenceAllowlistCreateRequest'>

export const silenceAllowlistKeys = {
  all: ['silence-allowlist'] as const,
  list: () => ['silence-allowlist', 'list'] as const,
}

export function useSilenceAllowlist(): UseQueryResult<SilenceAllowlistListResponse, ApiError> {
  return useQuery({
    queryKey: silenceAllowlistKeys.list(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures/silence-allowlist')
      return unwrap<SilenceAllowlistListResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

export function useCreateSilenceAllowlistEntry(): UseMutationResult<
  SilenceAllowlistResponse,
  ApiError,
  SilenceAllowlistCreateRequest
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body) => {
      const result = await apiClient.POST('/api/logs/signatures/silence-allowlist', { body })
      return unwrap<SilenceAllowlistResponse>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: silenceAllowlistKeys.all })
    },
  })
}

export function useDeleteSilenceAllowlistEntry(): UseMutationResult<void, ApiError, number> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (entryId) => {
      const result = await apiClient.DELETE('/api/logs/signatures/silence-allowlist/{entry_id}', {
        params: { path: { entry_id: entryId } },
      })
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: silenceAllowlistKeys.all })
    },
  })
}
