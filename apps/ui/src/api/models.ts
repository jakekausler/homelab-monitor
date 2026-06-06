import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type ModelListResponse = Schema<'ModelListResponse'>
export type ModelDetailResponse = Schema<'ModelDetailResponse'>
export type LastCycleResponse = Schema<'LastCycleResponse'>

export const modelKeys = {
  all: ['models'] as const,
  list: () => ['models', 'list'] as const,
  one: (k: string) => ['models', 'one', k] as const,
  lastCycle: () => ['models', 'cycle', 'last'] as const,
}

export function useModelsList(): UseQueryResult<ModelListResponse, ApiError> {
  return useQuery({
    queryKey: modelKeys.list(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures/models')
      return unwrap<ModelListResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

export function useModelDetail(
  modelKey: string,
  enabled: boolean,
): UseQueryResult<ModelDetailResponse, ApiError> {
  return useQuery({
    queryKey: modelKeys.one(modelKey),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures/models/{model_key}', {
        params: { path: { model_key: modelKey } },
      })
      return unwrap<ModelDetailResponse>(result)
    },
    enabled: enabled && modelKey.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

export function useLastCycle(): UseQueryResult<LastCycleResponse, ApiError> {
  return useQuery({
    queryKey: modelKeys.lastCycle(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/signatures/cycle/last')
      return unwrap<LastCycleResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}

/**
 * Trigger a manual drain refresh cycle. POST returns a cycle_id;
 * we then invalidate the lastCycle query so the footer refetches stats.
 * CSRF token is auto-attached by the apiClient middleware.
 */
export function useTriggerRefresh(): UseMutationResult<void, ApiError, void> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      // POST /api/logs/signatures/refresh returns { cycle_id: string }
      // We don't need the cycle_id for this simple integration; we just
      // invalidate lastCycle to refetch after ~3s (last_result is set synchronously).
      const result = await apiClient.POST('/api/logs/signatures/refresh')
      unwrap<{ cycle_id: string }>(result)
    },
    onSuccess: () => {
      // After refresh is triggered, invalidate so the footer refetches the new stats.
      // Deliberate simple integration for this debug surface: we invalidate after a
      // fixed ~3s rather than polling /refresh/{cycle_id} to completion. A cycle that
      // runs longer than 3s leaves the footer briefly stale; the 30s staleTime + the
      // periodic background cycle's own persisted last_result backstop that. If footer
      // freshness ever matters, switch to the existing poll-the-cycle_id flow.
      setTimeout(() => {
        void qc.invalidateQueries({ queryKey: modelKeys.lastCycle() })
      }, 3000)
    },
  })
}
