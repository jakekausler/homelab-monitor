import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'
import type { RunState } from '@/components/crons/badges'

type CronListResponse = Schema<'CronListResponse'>
type CronWithStateOut = Schema<'CronWithStateOut'>
type CronOut = Schema<'CronOut'>
type CronUpdate = Schema<'CronUpdate'>
type PreviewRunsResponse = Schema<'PreviewRunsResponse'>
type CronRunListResponse = Schema<'CronRunListResponse'>
type RunLogResponse = Schema<'RunLogResponse'>
export type InstallWrapperResponse =
  | Schema<'InstallWrapperPreview'>
  | Schema<'InstallWrapperResult'>
export type UninstallWrapperResponse =
  | Schema<'UninstallWrapperPreview'>
  | Schema<'UninstallWrapperResult'>

export interface CronListQuery {
  page?: number
  page_size?: number
  host?: string
  state?: 'unknown' | 'running' | 'ok' | 'failed' | 'late'
  wrapper_installed?: boolean
  q?: string
  include_hidden?: boolean
  include_soft_deleted?: boolean
}

export interface CronRunListQuery {
  limit?: number
  cursor?: string
  state?: RunState
}

export const cronQueryKeys = {
  all: ['crons'] as const,
  list: (query: CronListQuery) => ['crons', 'list', query] as const,
  detail: (id: string) => ['crons', 'detail', id] as const,
  previewSaved: (id: string, count: number) => ['crons', 'preview', 'saved', id, count] as const,
  previewExpr: (expr: string, count: number) => ['crons', 'preview', 'expr', expr, count] as const,
  runs: (fingerprint: string, query: CronRunListQuery) =>
    ['crons', 'runs', fingerprint, query] as const,
  runLog: (fingerprint: string, runId: string) => ['crons', 'run-log', fingerprint, runId] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useListCrons(query: CronListQuery): UseQueryResult<CronListResponse, ApiError> {
  return useQuery({
    queryKey: cronQueryKeys.list(query),
    queryFn: async () => {
      const result = await apiClient.GET('/api/crons', {
        params: { query },
      })
      return unwrap<CronListResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useGetCron(
  id: string,
  options: { includeHidden?: boolean } = {},
): UseQueryResult<CronWithStateOut, ApiError> {
  return useQuery({
    queryKey: cronQueryKeys.detail(id),
    queryFn: async () => {
      const result = await apiClient.GET('/api/crons/{fingerprint}', {
        params: {
          path: { fingerprint: id },
          query: { include_hidden: options.includeHidden ?? false },
        },
      })
      return unwrap<CronWithStateOut>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
    enabled: id.length > 0,
  })
}

export function useUpdateCron(id: string): UseMutationResult<CronOut, ApiError, CronUpdate> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: CronUpdate) => {
      const result = await apiClient.PATCH('/api/crons/{fingerprint}', {
        params: { path: { fingerprint: id } },
        body,
      })
      return unwrap<CronOut>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cronQueryKeys.all })
      void qc.invalidateQueries({ queryKey: cronQueryKeys.detail(id) })
    },
  })
}

export function useHideCron(id: string): UseMutationResult<void, ApiError, void> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const result = await apiClient.DELETE('/api/crons/{fingerprint}', {
        params: { path: { fingerprint: id } },
      })
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cronQueryKeys.all })
      void qc.invalidateQueries({ queryKey: cronQueryKeys.detail(id) })
    },
  })
}

export function usePreviewSavedCron(
  id: string,
  count: number = 3,
  enabled: boolean = true,
): UseQueryResult<PreviewRunsResponse, ApiError> {
  return useQuery({
    queryKey: cronQueryKeys.previewSaved(id, count),
    queryFn: async () => {
      const result = await apiClient.GET('/api/crons/{fingerprint}/preview-runs', {
        params: { path: { fingerprint: id }, query: { count } },
      })
      return unwrap<PreviewRunsResponse>(result)
    },
    enabled: enabled && id.length > 0,
  })
}

export function usePreviewExpr(
  expr: string,
  count: number = 3,
  enabled: boolean = true,
): UseQueryResult<PreviewRunsResponse, ApiError> {
  return useQuery({
    queryKey: cronQueryKeys.previewExpr(expr, count),
    queryFn: async () => {
      const result = await apiClient.GET('/api/crons/preview-runs', {
        params: { query: { expr, count } },
      })
      return unwrap<PreviewRunsResponse>(result)
    },
    enabled: enabled && expr.trim().length > 0,
    retry: false,
  })
}

export interface DiscoverResponse {
  found_count: number
  error_count: number
  soft_deleted_count?: number
  restored_count?: number
}

export function useDiscoverNow(): UseMutationResult<DiscoverResponse, ApiError, void> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const result = await apiClient.POST('/api/crons/discover-now', {})
      return unwrap<DiscoverResponse>(result as never)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cronQueryKeys.all })
    },
  })
}

export function useInstallWrapper(
  id: string,
): UseMutationResult<InstallWrapperResponse, ApiError, { confirm: boolean }> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ confirm }) => {
      const result = await apiClient.POST('/api/crons/{fingerprint}/install-wrapper', {
        params: { path: { fingerprint: id } },
        body: { confirm },
      })
      return unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cronQueryKeys.all })
      void qc.invalidateQueries({ queryKey: cronQueryKeys.detail(id) })
    },
  })
}

export function useUninstallWrapper(
  id: string,
): UseMutationResult<UninstallWrapperResponse, ApiError, { confirm: boolean }> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ confirm }) => {
      const result = await apiClient.POST('/api/crons/{fingerprint}/uninstall-wrapper', {
        params: { path: { fingerprint: id } },
        body: { confirm },
      })
      return unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: cronQueryKeys.all })
      void qc.invalidateQueries({ queryKey: cronQueryKeys.detail(id) })
    },
  })
}

export function useListCronRuns(
  fingerprint: string,
  query: CronRunListQuery = {},
): UseQueryResult<CronRunListResponse, ApiError> {
  return useQuery({
    queryKey: cronQueryKeys.runs(fingerprint, query),
    queryFn: async () => {
      const result = await apiClient.GET('/api/crons/{fingerprint}/runs', {
        params: {
          path: { fingerprint },
          query: {
            ...(query.limit !== undefined && { limit: query.limit }),
            ...(query.cursor !== undefined && { cursor: query.cursor }),
            ...(query.state !== undefined && { state: query.state }),
          },
        },
      })
      return unwrap<CronRunListResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
    enabled: fingerprint.length > 0,
  })
}

export function useCronRunLog(
  fingerprint: string,
  runId: string,
): UseInfiniteQueryResult<
  { pages: RunLogResponse[]; pageParams: (string | undefined)[] },
  ApiError
> {
  return useInfiniteQuery({
    queryKey: cronQueryKeys.runLog(fingerprint, runId),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const query: Record<string, string> = {}
      if (pageParam) query.cursor = pageParam
      const result = await apiClient.GET('/api/crons/{fingerprint}/runs/{run_id}/log', {
        params: { path: { fingerprint, run_id: runId }, query },
      })
      return unwrap<RunLogResponse>(result)
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: fingerprint.length > 0 && runId.length > 0,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.pages[0]?.log_status === 'running' ? 5000 : false,
  })
}
