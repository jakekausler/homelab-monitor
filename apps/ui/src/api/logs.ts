import { useInfiniteQuery } from '@tanstack/react-query'

import { apiClient, unwrap } from './client'
import type { Schema } from './types'

type LogsQueryResponse = Schema<'LogsQueryResponse'>

export const logsQueryKeys = {
  query: (expr: string, start: string, end: string) => ['logs', 'query', expr, start, end] as const,
}

/**
 * STAGE-004-007 — generic LogsQL query with A1 cursor pagination.
 * useInfiniteQuery; flatten via data.pages.flatMap(p => p.lines).
 */
export function useLogsQuery(expr: string, start: string, end: string) {
  return useInfiniteQuery({
    queryKey: logsQueryKeys.query(expr, start, end),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const result = await apiClient.GET('/api/logs/query', {
        params: { query: { expr, start, end, ...(pageParam ? { cursor: pageParam } : {}) } },
      })
      return unwrap<LogsQueryResponse>(result)
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: expr.length > 0 && start.length > 0 && end.length > 0,
    retry: false,
  })
}
