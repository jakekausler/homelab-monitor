import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

export type SavedQueriesListResponse = Schema<'SavedQueriesListResponse'>
export type SavedQuery = Schema<'SavedQueryResponse'>
export type SaveQueryCreateRequest = Schema<'SaveQueryCreateRequest'>
export type SaveQueryRenameRequest = Schema<'SaveQueryRenameRequest'>

export const savedLogQueryKeys = {
  all: ['saved-log-queries'] as const,
  list: () => ['saved-log-queries', 'list'] as const,
}

export function useSavedLogQueriesQuery(): UseQueryResult<SavedQueriesListResponse, ApiError> {
  return useQuery({
    queryKey: savedLogQueryKeys.list(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/saved-queries', {})
      return unwrap<SavedQueriesListResponse>(result)
    },
    retry: false,
  })
}

export function useCreateSavedLogQuery(): UseMutationResult<
  SavedQuery,
  ApiError,
  SaveQueryCreateRequest
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: SaveQueryCreateRequest) => {
      const result = await apiClient.POST('/api/logs/saved-queries', { body })
      return unwrap<SavedQuery>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: savedLogQueryKeys.all })
    },
  })
}

export function useRenameSavedLogQuery(): UseMutationResult<
  SavedQuery,
  ApiError,
  { id: number; name: string }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, name }) => {
      const body: SaveQueryRenameRequest = { name }
      const result = await apiClient.PATCH('/api/logs/saved-queries/{query_id}', {
        params: { path: { query_id: id } },
        body,
      })
      return unwrap<SavedQuery>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: savedLogQueryKeys.all })
    },
  })
}

export function useDeleteSavedLogQuery(): UseMutationResult<void, ApiError, { id: number }> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id }) => {
      const result = await apiClient.DELETE('/api/logs/saved-queries/{query_id}', {
        params: { path: { query_id: id } },
      })
      if (result.response.status === 204) return
      unwrap(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: savedLogQueryKeys.all })
    },
  })
}

export function useUpdateSavedLogQuery(): UseMutationResult<
  SavedQuery,
  ApiError,
  { id: number; body: SaveQueryCreateRequest }
> {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, body }) => {
      const result = await apiClient.PUT('/api/logs/saved-queries/{query_id}', {
        params: { path: { query_id: id } },
        body,
      })
      return unwrap<SavedQuery>(result)
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: savedLogQueryKeys.all })
    },
  })
}

const COPY_SUFFIX_RE = /^(.*?)\s*\(copy(?:\s+\d+)?\)$/

export function computeCopyName(sourceName: string, existingNames: readonly string[]): string {
  const match = COPY_SUFFIX_RE.exec(sourceName)
  const base = match ? match[1]!.trim() : sourceName
  const taken = new Set(existingNames)
  // First candidate is "(copy)" with no number; then "(copy 1)", "(copy 2)", ...
  let candidate = `${base} (copy)`
  if (!taken.has(candidate)) return candidate
  let n = 1
  for (;;) {
    candidate = `${base} (copy ${n})`
    if (!taken.has(candidate)) return candidate
    n += 1
  }
}

export function savedRowToCreateRequest(row: SavedQuery, newName: string): SaveQueryCreateRequest {
  const base: SaveQueryCreateRequest = {
    name: newName,
    logs_ql: row.logs_ql,
    selected_services: row.selected_services.map((s) => ({
      service: s.service,
      source_type: s.source_type,
    })),
    advanced_mode: row.advanced_mode,
  }
  if (row.since_preset != null) {
    return { ...base, since_preset: row.since_preset }
  }
  if (row.range_start_iso != null && row.range_end_iso != null) {
    return {
      ...base,
      range_start_iso: row.range_start_iso,
      range_end_iso: row.range_end_iso,
    }
  }
  // Defensive: a stored row always satisfies the invariant, but if somehow
  // neither is set, fall through with the base (the backend validator will 422).
  return base
}
