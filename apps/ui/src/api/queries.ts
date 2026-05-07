import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { components } from './schema'

type MeResponse = components['schemas']['MeResponse']
type LoginRequest = components['schemas']['LoginRequest']
type LoginResponse = components['schemas']['LoginResponse']
type VersionResponse = components['schemas']['VersionResponse']
type CollectorStatus = components['schemas']['CollectorStatus']
type MetricsSnapshotResponse = components['schemas']['MetricsSnapshotResponse']
type AlertListResponse = components['schemas']['AlertListResponse']

export const queryKeys = {
  currentUser: ['auth', 'me'] as const,
  version: ['version'] as const,
  collectors: ['collectors'] as const,
  metricsSnapshot: ['metrics', 'snapshot'] as const,
  alerts: (params: { status?: 'firing' | 'resolved' }) => ['alerts', params] as const,
}

/**
 * Fetch the current authenticated user. Returns `null` on 401 so route
 * guards can use the result directly without try/catch.
 */
export function useCurrentUser(): UseQueryResult<MeResponse | null, ApiError> {
  return useQuery({
    queryKey: queryKeys.currentUser,
    queryFn: async (): Promise<MeResponse | null> => {
      const result = await apiClient.GET('/api/auth/me')
      if (result.response.status === 401) {
        return null
      }
      return unwrap(result)
    },
    retry: false,
    staleTime: 30_000,
  })
}

export function useVersion(): UseQueryResult<VersionResponse, ApiError> {
  return useQuery({
    queryKey: queryKeys.version,
    queryFn: async () => {
      const result = await apiClient.GET('/api/version')
      return unwrap(result)
    },
    staleTime: Infinity,
    retry: 1,
  })
}

export function useCollectors(): UseQueryResult<CollectorStatus[], ApiError> {
  return useQuery({
    queryKey: queryKeys.collectors,
    queryFn: async () => {
      const result = await apiClient.GET('/api/collectors')
      return unwrap(result)
    },
    refetchInterval: 30_000,
  })
}

export function useMetricsSnapshot(): UseQueryResult<MetricsSnapshotResponse, ApiError> {
  return useQuery({
    queryKey: queryKeys.metricsSnapshot,
    queryFn: async () => {
      const result = await apiClient.GET('/api/metrics/snapshot')
      return unwrap(result)
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })
}

export function useAlerts(
  params: { status?: 'firing' | 'resolved' } = {},
): UseQueryResult<AlertListResponse, ApiError> {
  return useQuery({
    queryKey: queryKeys.alerts(params),
    queryFn: async () => {
      const result = await apiClient.GET('/api/alerts', {
        params: { query: params },
      })
      return unwrap(result)
    },
  })
}

export function useLogin(): UseMutationResult<LoginResponse, ApiError, LoginRequest> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: LoginRequest) => {
      const result = await apiClient.POST('/api/auth/login', { body })
      return unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.currentUser })
    },
  })
}

export function useLogout(): UseMutationResult<void, ApiError, void> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const result = await apiClient.POST('/api/auth/logout')
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: () => {
      qc.setQueryData(queryKeys.currentUser, null)
      void qc.invalidateQueries({ queryKey: queryKeys.currentUser })
    },
  })
}
