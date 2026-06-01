import { useInfiniteQuery, useQuery } from '@tanstack/react-query'

import { apiClient, unwrap } from './client'
import type { Schema } from './types'

type LogsQueryResponse = Schema<'LogsQueryResponse'>
type LogsServicesResponse = Schema<'LogsServicesResponse'>

export const logsQueryKeys = {
  query: (expr: string, start: string, end: string, services: string) =>
    ['logs', 'query', expr, start, end, services] as const,
  services: (start: string, end: string, limit: number) =>
    ['logs', 'services', start, end, limit] as const,
}

/**
 * STAGE-004-007 / -012 — generic LogsQL query with A1 cursor pagination.
 * `services` is a CSV of selected service values; the BACKEND composes the
 * (service:…) AND (expr) filter. Included in the query key so changing the
 * selection refetches.
 */
export function useLogsQuery(expr: string, start: string, end: string, services = '') {
  return useInfiniteQuery({
    queryKey: logsQueryKeys.query(expr, start, end, services),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const result = await apiClient.GET('/api/logs/query', {
        params: {
          query: {
            expr,
            start,
            end,
            ...(services.length > 0 ? { services } : {}),
            ...(pageParam ? { cursor: pageParam } : {}),
          },
        },
      })
      return unwrap<LogsQueryResponse>(result)
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: expr.length > 0 && start.length > 0 && end.length > 0,
    retry: false,
  })
}

/**
 * STAGE-004-012 — distinct `service` values + counts for the stream picker.
 * Depends on (start, end, limit) ONLY — counts reflect the window, NOT expr or
 * the current selection. 30s staleTime to match the backend cache TTL.
 */
const SERVICES_DEFAULT_LIMIT = 100

export function useLogsServicesQuery(start: string, end: string, limit = SERVICES_DEFAULT_LIMIT) {
  return useQuery({
    queryKey: logsQueryKeys.services(start, end, limit),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/services', {
        params: { query: { start, end, limit } },
      })
      return unwrap<LogsServicesResponse>(result)
    },
    // Guarded for reusability: callers may pass empty start/end before a range is resolved.
    enabled: start.length > 0 && end.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}
