import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type PiholeOverviewResponse = Schema<'PiholeOverviewResponse'>

export const piholeQueryKeys = {
  overview: ['integrations', 'pihole', 'overview'] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function usePiholeOverview(): UseQueryResult<PiholeOverviewResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.overview,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/overview', {})
      return unwrap<PiholeOverviewResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
