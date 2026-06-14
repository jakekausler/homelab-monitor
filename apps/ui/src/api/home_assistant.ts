import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { components } from './schema'
import type { Schema as SchemaAlias } from './types'

type HaSummaryResponse = SchemaAlias<'HaSummaryResponse'>
type HaEntityRowsResponse = components['schemas']['HaEntityRowsResponse']
type HaBatteryRowsResponse = components['schemas']['HaBatteryRowsResponse']
type HaUpdateRowsResponse = components['schemas']['HaUpdateRowsResponse']
type HaConfigEntryRowsResponse = components['schemas']['HaConfigEntryRowsResponse']
type HaRepairRowsResponse = components['schemas']['HaRepairRowsResponse']

export const haQueryKeys = {
  summary: ['integrations', 'home-assistant', 'summary'] as const,
  entities: (filter: string) => ['integrations', 'home-assistant', 'entities', filter] as const,
  batteries: (filter: string) => ['integrations', 'home-assistant', 'batteries', filter] as const,
  updates: ['integrations', 'home-assistant', 'updates'] as const,
  configEntries: (filter: string) =>
    ['integrations', 'home-assistant', 'config-entries', filter] as const,
  repairs: ['integrations', 'home-assistant', 'repairs'] as const,
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

export function useHomeAssistantEntities(
  filter = 'unavailable',
): UseQueryResult<HaEntityRowsResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.entities(filter),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/entities', {
        params: { query: { filter } },
      })
      return unwrap<HaEntityRowsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useHomeAssistantBatteries(
  filter = 'low_or_critical',
): UseQueryResult<HaBatteryRowsResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.batteries(filter),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/batteries', {
        params: { query: { filter } },
      })
      return unwrap<HaBatteryRowsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useHomeAssistantUpdates(): UseQueryResult<HaUpdateRowsResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.updates,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/updates', {})
      return unwrap<HaUpdateRowsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useHomeAssistantConfigEntries(
  filter = 'error',
): UseQueryResult<HaConfigEntryRowsResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.configEntries(filter),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/config-entries', {
        params: { query: { filter } },
      })
      return unwrap<HaConfigEntryRowsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useHomeAssistantRepairs(): UseQueryResult<HaRepairRowsResponse, ApiError> {
  return useQuery({
    queryKey: haQueryKeys.repairs,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/home-assistant/repairs', {})
      return unwrap<HaRepairRowsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}
