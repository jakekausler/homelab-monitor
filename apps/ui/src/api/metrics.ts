import { useQuery } from '@tanstack/react-query'

import { apiClient, unwrap } from './client'
import type { Schema } from './types'

type MetricNamesResponse = Schema<'MetricNamesResponse'>

export const metricsQueryKeys = {
  metricNames: () => ['metrics', 'metric-names'] as const,
}

/**
 * STAGE-005-005 — distinct VictoriaMetrics metric names for the MetricsQL
 * Simple-mode authoring autocomplete. No time range (VM's `__name__` label
 * values are global). 30s staleTime; advisory-only so `retry: false`. Always
 * enabled — the result is cheap and shared across modal opens via the query key.
 */
export function useMetricNamesQuery() {
  return useQuery({
    queryKey: metricsQueryKeys.metricNames(),
    queryFn: async () => {
      const result = await apiClient.GET('/api/metrics/metric-names')
      return unwrap<MetricNamesResponse>(result)
    },
    staleTime: 30_000,
    retry: false,
  })
}
