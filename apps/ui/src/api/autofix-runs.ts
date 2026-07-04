import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, type ApiError, unwrap } from './client'
import type { Schema } from './types'

export type Run = Schema<'RunOut'>
export type RunDetail = Schema<'RunDetailOut'>
export type RunFeedback = Schema<'RunFeedbackOut'>
export type Transcript = Schema<'TranscriptOut'>

export interface RunsListFilters {
  runbook_id: string | undefined
  mode: 'dry_run' | 'real' | undefined
  outcome: 'in_flight' | 'success' | 'failure' | 'killed' | undefined
  initiator: 'alert' | 'operator' | undefined
  since: string | undefined // ISO 8601 UTC
  until: string | undefined // ISO 8601 UTC
}

export const RUNS_PAGE_SIZE = 100

export const autofixRunsKeys = {
  all: ['autofix-runs'] as const,
  list: (filters: RunsListFilters, page: number) =>
    ['autofix-runs', 'list', filters, page] as const,
  detail: (id: string) => ['autofix-runs', 'detail', id] as const,
  feedback: (id: string) => ['autofix-runs', 'feedback', id] as const,
  transcript: (id: string) => ['autofix-runs', 'transcript', id] as const,
} as const

/** GET /api/autofix/runs */
export function useRunsList(
  filters: RunsListFilters,
  page: number,
): UseQueryResult<{ items: Run[]; total_count: number; limit: number; offset: number }, ApiError> {
  const offset = Math.max(0, (page - 1) * RUNS_PAGE_SIZE)
  return useQuery({
    queryKey: autofixRunsKeys.list(filters, page),
    queryFn: async () => {
      const result = await apiClient.GET('/api/autofix/runs', {
        params: {
          query: {
            runbook_id: filters.runbook_id ?? null,
            mode: filters.mode ?? null,
            outcome: filters.outcome ?? null,
            initiator: filters.initiator ?? null,
            since: filters.since ?? null,
            until: filters.until ?? null,
            limit: RUNS_PAGE_SIZE,
            offset,
          },
        },
      })
      return unwrap<{ items: Run[]; total_count: number; limit: number; offset: number }>(result)
    },
    retry: false,
  })
}

/** GET /api/autofix/runs/{run_id} */
export function useRun(runId: string): UseQueryResult<RunDetail, ApiError> {
  return useQuery({
    queryKey: autofixRunsKeys.detail(runId),
    queryFn: async () => {
      const result = await apiClient.GET('/api/autofix/runs/{run_id}', {
        params: { path: { run_id: runId } },
      })
      return unwrap<RunDetail>(result)
    },
    retry: false,
    enabled: runId !== '',
  })
}

/** GET /api/autofix/runs/{run_id}/feedback */
export function useRunFeedback(runId: string): UseQueryResult<{ items: RunFeedback[] }, ApiError> {
  return useQuery({
    queryKey: autofixRunsKeys.feedback(runId),
    queryFn: async () => {
      const result = await apiClient.GET('/api/autofix/runs/{run_id}/feedback', {
        params: { path: { run_id: runId } },
      })
      return unwrap<{ items: RunFeedback[] }>(result)
    },
    retry: false,
    enabled: runId !== '',
  })
}

/** GET /api/autofix/runs/{run_id}/transcript */
export function useRunTranscript(runId: string): UseQueryResult<Transcript, ApiError> {
  return useQuery({
    queryKey: autofixRunsKeys.transcript(runId),
    queryFn: async () => {
      const result = await apiClient.GET('/api/autofix/runs/{run_id}/transcript', {
        params: { path: { run_id: runId } },
      })
      return unwrap<Transcript>(result)
    },
    retry: false,
    enabled: runId !== '',
  })
}
