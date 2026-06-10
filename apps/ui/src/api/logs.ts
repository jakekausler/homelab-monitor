import { useInfiniteQuery, useQuery } from '@tanstack/react-query'

import { apiClient, unwrap } from './client'
import type { LogLine } from '@/components/logs/types'
import type { Schema } from './types'

type LogsQueryResponse = Schema<'LogsQueryResponse'>
type LogsServicesResponse = Schema<'LogsServicesResponse'>
type LogsFieldsResponse = Schema<'LogsFieldsResponse'>
type LogsHistogramResponse = Schema<'LogsHistogramResponse'>
type LogWindowResponse = Schema<'LogWindowResponse'>

/**
 * STAGE-004-012A — a selected stream picker identity. The identity is the PAIR
 * (service, source_type); the same service name may be selectable under multiple
 * source_types. Serialized to the URL/query CSV as `<source_type>:<service>`.
 * Kept serializable for STAGE-015 persistence.
 */
export type ServiceIdentity = { service: string; source_type: string }

/** Serialize identities to the backend `services` CSV: `type:service,type:service`. */
export function identitiesToServicesCsv(identities: ServiceIdentity[]): string {
  return identities.map((i) => `${i.source_type}:${i.service}`).join(',')
}

/**
 * STAGE-004-018B — serialize the ordered column list to the `columns` URL CSV.
 * Field names contain no commas (safe). Empty list → '' (caller omits the key).
 */
export function columnsToCsv(cols: string[]): string {
  return cols.join(',')
}

/**
 * STAGE-004-018B — parse the `columns` URL param (CSV of field names) into an
 * ordered string[]. Splits on ',', trims each, drops empties. Already-array
 * input is filtered to strings. Empty/absent/no-valid-entries → undefined (so
 * the URL key is omitted and validateSearch returns undefined).
 */
export function parseColumnsParam(raw: unknown): string[] | undefined {
  let parts: string[]
  if (typeof raw === 'string') {
    parts = raw.split(',')
  } else if (Array.isArray(raw)) {
    parts = (raw as unknown[]).filter((s): s is string => typeof s === 'string')
  } else {
    return undefined
  }
  const out = parts.map((s) => s.trim()).filter((s) => s.length > 0)
  return out.length > 0 ? out : undefined
}

export const logsQueryKeys = {
  query: (expr: string, start: string, end: string, services: string) =>
    ['logs', 'query', expr, start, end, services] as const,
  services: (start: string, end: string, limit: number) =>
    ['logs', 'services', start, end, limit] as const,
  fields: (expr: string, start: string, end: string, services: string, sample: number) =>
    ['logs', 'fields', expr, start, end, services, sample] as const,
  histogram: (expr: string, start: string, end: string, buckets: number, services: string) =>
    ['logs', 'histogram', expr, start, end, buckets, services] as const,
  window: (
    anchorTs: string,
    anchorStream: string,
    anchorMessage: string,
    expr: string,
    scope: string, // '' for all, or 'source_type:service'
  ) => ['logs', 'window', anchorTs, anchorStream, anchorMessage, expr, scope] as const,
}

/**
 * STAGE-004-007 / -012 — generic LogsQL query with A1 cursor pagination.
 * `services` is a CSV of <source_type>:<service> entries (e.g. `docker:nginx,cron:hmrun`);
 * the BACKEND parses and composes the identity-qualified filter. Included in the query key
 * so changing the selection refetches.
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
 * STAGE-004-024 — window-shift "Load newer" fetch. NO backend change:
 * /api/logs/query returns the LATEST N lines in [start,end], so Load-newer is a
 * one-shot query over [newestShownTimestamp, end]. Caller dedupes against the
 * current buffer. Known limitation: if >page-size new lines accumulated since
 * `start`, the oldest of that gap are skipped (converges via repeat / tail).
 */
export async function fetchNewerLogs(
  expr: string,
  start: string,
  end: string,
  services = '',
): Promise<LogLine[]> {
  const result = await apiClient.GET('/api/logs/query', {
    params: {
      query: {
        expr,
        start,
        end,
        ...(services.length > 0 ? { services } : {}),
      },
    },
  })
  return unwrap<LogsQueryResponse>(result).lines
}

/**
 * STAGE-004-031A (in-place surrounding mode) — "Load older" one-shot.
 * /api/logs/query returns the LATEST N lines in [start,end] (presented
 * oldest→newest by the backend). For load-older over [olderStart, end=earliest],
 * this yields the N lines immediately OLDER than `end`. Caller filters the
 * boundary line + prepends.
 */
export async function fetchOlderLogs(
  expr: string,
  start: string,
  end: string,
  services = '',
): Promise<LogLine[]> {
  const result = await apiClient.GET('/api/logs/query', {
    params: {
      query: {
        expr,
        start,
        end,
        ...(services.length > 0 ? { services } : {}),
      },
    },
  })
  return unwrap<LogsQueryResponse>(result).lines
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

/**
 * STAGE-004-018 — discover fields present in the current scope. Mirrors
 * useLogsServicesQuery: typed apiClient.GET, 30s staleTime (matches backend
 * cache TTL), enabled only when a window is resolved. `services` is the CSV
 * `<source_type>:<service>` form (same as useLogsQuery); the backend composes it.
 */
const FIELDS_DEFAULT_SAMPLE = 200

export function useLogsFieldsQuery(
  expr: string,
  start: string,
  end: string,
  services = '',
  sample = FIELDS_DEFAULT_SAMPLE,
) {
  return useQuery({
    queryKey: logsQueryKeys.fields(expr, start, end, services, sample),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/fields', {
        params: {
          query: {
            expr,
            start,
            end,
            sample_n: sample,
            ...(services.length > 0 ? { services } : {}),
          },
        },
      })
      return unwrap<LogsFieldsResponse>(result)
    },
    enabled: expr.length > 0 && start.length > 0 && end.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

/**
 * STAGE-004-019 — severity-stacked log-density histogram for the current scope.
 * Mirrors useLogsFieldsQuery: typed apiClient.GET, 30s staleTime (matches the
 * backend cache TTL), enabled only when a window is resolved. `services` is the
 * CSV `<source_type>:<service>` form; the backend composes it.
 */
const HISTOGRAM_DEFAULT_BUCKETS = 60

export function useLogsHistogramQuery(
  expr: string,
  start: string,
  end: string,
  buckets = HISTOGRAM_DEFAULT_BUCKETS,
  services = '',
) {
  return useQuery({
    queryKey: logsQueryKeys.histogram(expr, start, end, buckets, services),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/histogram', {
        params: {
          query: {
            expr,
            start,
            end,
            buckets,
            ...(services.length > 0 ? { services } : {}),
          },
        },
      })
      return unwrap<LogsHistogramResponse>(result)
    },
    enabled: expr.length > 0 && start.length > 0 && end.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}

/**
 * STAGE-004-031A — surrounding-logs window for a selected line. Fetches N before
 * + N after the anchor's timestamp (backend calls LogWindowFetcher twice +
 * merges). `service`/`sourceType` empty → ALL-services scope; both present →
 * only-this-service scope. enabled only when an anchor timestamp is provided.
 */
const SURROUNDING_DEFAULT_COUNT = 100

export function useSurroundingLogs(args: {
  anchorTs: string
  anchorStream: string
  anchorMessage: string
  expr: string
  service?: string
  sourceType?: string
  before?: number
  after?: number
  enabled?: boolean
}) {
  const {
    anchorTs,
    anchorStream,
    anchorMessage,
    expr,
    service,
    sourceType,
    before = SURROUNDING_DEFAULT_COUNT,
    after = SURROUNDING_DEFAULT_COUNT,
    enabled = true,
  } = args
  const scope =
    service !== undefined && service.length > 0 ? `${sourceType ?? 'unknown'}:${service}` : ''
  return useQuery({
    queryKey: logsQueryKeys.window(anchorTs, anchorStream, anchorMessage, expr, scope),
    queryFn: async () => {
      const result = await apiClient.GET('/api/logs/window', {
        params: {
          query: {
            anchor_ts: anchorTs,
            anchor_stream: anchorStream,
            anchor_message: anchorMessage,
            expr,
            before,
            after,
            ...(service !== undefined && service.length > 0
              ? { service, source_type: sourceType ?? 'unknown' }
              : {}),
          },
        },
      })
      return unwrap<LogWindowResponse>(result)
    },
    enabled: enabled && anchorTs.length > 0,
    staleTime: 30_000,
    retry: false,
  })
}
