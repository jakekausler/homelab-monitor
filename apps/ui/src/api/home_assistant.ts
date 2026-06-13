import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type HaSummaryResponse = Schema<'HaSummaryResponse'>

export const haQueryKeys = {
  summary: ['integrations', 'home-assistant', 'summary'] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useHomeAssistantSummary(): UseQueryResult<HaSummaryResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/summary', {})
      return unwrap<HaSummaryResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
