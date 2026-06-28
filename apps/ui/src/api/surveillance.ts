import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type SurveillanceSummary = Schema<'SurveillanceSummary'>
type SurveillanceCameras = Schema<'SurveillanceCameras'>

export const surveillanceQueryKeys = {
  summary: ['integrations', 'surveillance', 'summary'] as const,
  cameras: ['integrations', 'surveillance', 'cameras'] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useSurveillanceSummary(): UseQueryResult<SurveillanceSummary, ApiError> {
  return useQuery({
    queryKey: surveillanceQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/surveillance/summary', {})
      return unwrap<SurveillanceSummary>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useSurveillanceCameras(): UseQueryResult<SurveillanceCameras, ApiError> {
  return useQuery({
    queryKey: surveillanceQueryKeys.cameras,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/surveillance/cameras', {})
      return unwrap<SurveillanceCameras>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
