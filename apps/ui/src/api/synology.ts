import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type SynologySummary = Schema<'SynologySummary'>

export const synologyQueryKeys = {
  summary: ['integrations', 'synology', 'summary'] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useSynologySummary(): UseQueryResult<SynologySummary, ApiError> {
  return useQuery({
    queryKey: synologyQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/synology/summary', {})
      return unwrap<SynologySummary>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
