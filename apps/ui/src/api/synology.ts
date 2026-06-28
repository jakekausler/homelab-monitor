import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type SynologySummary = Schema<'SynologySummary'>
type SynologyHardware = Schema<'SynologyHardware'>
type SynologyDiskSmartAttrs = Schema<'SynologyDiskSmartAttrs'>

export const synologyQueryKeys = {
  summary: ['integrations', 'synology', 'summary'] as const,
  hardware: ['integrations', 'synology', 'hardware'] as const,
  diskSmart: (disk: string) => ['integrations', 'synology', 'disk-smart', disk] as const,
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

export function useSynologyHardware(): UseQueryResult<SynologyHardware, ApiError> {
  return useQuery({
    queryKey: synologyQueryKeys.hardware,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/synology/hardware', {})
      return unwrap<SynologyHardware>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useSynologyDiskSmart(
  disk: string,
): UseQueryResult<SynologyDiskSmartAttrs, ApiError> {
  return useQuery({
    queryKey: synologyQueryKeys.diskSmart(disk),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/synology/disks/{disk}/smart-attrs', {
        params: { path: { disk } },
      })
      return unwrap<SynologyDiskSmartAttrs>(result)
    },
    enabled: disk.length > 0,
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
