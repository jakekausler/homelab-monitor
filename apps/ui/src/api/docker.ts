import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type ContainerListResponse = Schema<'ContainerListResponse'>
type DockerSuggestionListResponse = Schema<'DockerSuggestionListResponse'>

export const dockerQueryKeys = {
  containers: ['integrations', 'docker', 'containers'] as const,
  suggestions: (status: string) => ['integrations', 'docker', 'suggestions', status] as const,
}

const REFETCH_INTERVAL_MS = 30_000

export function useListContainers(): UseQueryResult<ContainerListResponse, ApiError> {
  return useQuery({
    queryKey: dockerQueryKeys.containers,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/containers', {})
      return unwrap<ContainerListResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export type DockerSuggestionStatus = 'pending' | 'accepted' | 'ignored' | 'container_gone' | 'all'

export function useListDockerSuggestions(
  status: DockerSuggestionStatus = 'pending',
): UseInfiniteQueryResult<
  { pages: DockerSuggestionListResponse[]; pageParams: (string | undefined)[] },
  ApiError
> {
  return useInfiniteQuery({
    queryKey: dockerQueryKeys.suggestions(status),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const query: Record<string, string> = { status }
      if (pageParam) query.cursor = pageParam
      const result = await apiClient.GET('/api/integrations/docker/suggestions', {
        params: { query },
      })
      return unwrap<DockerSuggestionListResponse>(result)
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

type ListProbesResponse = Schema<'ListProbesResponse'>
type ProbeRowSchema = Schema<'ProbeRow'>
type ProbeSummaryResponse = Schema<'ProbeSummaryResponse'>
type ImageUpdateSummaryResponse = Schema<'ImageUpdateSummaryResponse'>
type ImageUpdateDetail = Schema<'ImageUpdateDetail'>

export const dockerProbeQueryKeys = {
  list: (containerName: string) =>
    ['integrations', 'docker', 'containers', containerName, 'probes'] as const,
  summary: ['integrations', 'docker', 'probes-summary'] as const,
}

const PROBE_REFETCH_INTERVAL_MS = 10_000
const PROBE_SUMMARY_REFETCH_INTERVAL_MS = 10_000

export type ProbeSummaryEntry = {
  container_name: string
  active: number
  failing: number
  config_errors?: string[] | null
  source_breakdown: Record<string, number>
}

export type ProbeSummary = Record<
  string,
  {
    active: number
    failing: number
    config_errors?: string[] | null
    source_breakdown: Record<string, number>
  }
>

/**
 * Fetch probe counts for ALL containers in one query. Use in the docker grid
 * to avoid N+1 — formerly each row called useListProbes(name) per container.
 */
export function useProbesSummary() {
  return useQuery({
    queryKey: dockerProbeQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/probes/summary', {})
      const payload = unwrap<ProbeSummaryResponse>(result)
      const summary: ProbeSummary = {}
      for (const entry of payload.summaries) {
        summary[entry.container_name] = {
          active: entry.active,
          failing: entry.failing,
          config_errors: entry.config_errors ?? null,
          source_breakdown: entry.source_breakdown,
        }
      }
      return summary
    },
    refetchInterval: PROBE_SUMMARY_REFETCH_INTERVAL_MS,
    staleTime: 5000,
  })
}

export function useListProbes(containerName: string): UseQueryResult<ListProbesResponse, ApiError> {
  return useQuery({
    queryKey: dockerProbeQueryKeys.list(containerName),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/containers/{name}/probes', {
        params: { path: { name: containerName } },
      })
      return unwrap<ListProbesResponse>(result)
    },
    refetchInterval: PROBE_REFETCH_INTERVAL_MS,
    enabled: containerName.length > 0,
  })
}

export function useToggleProbe() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ probeId, enabled }: { probeId: string; enabled: boolean }) => {
      const path = enabled
        ? '/api/integrations/docker/probes/{probe_id}/enable'
        : '/api/integrations/docker/probes/{probe_id}/disable'
      const result = await apiClient.POST(path, {
        params: { path: { probe_id: probeId } },
      })
      return unwrap<ProbeRowSchema>(result)
    },
    onSuccess: (row) => {
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.list(row.container_name),
      })
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.summary,
      })
    },
  })
}

export const dockerImageUpdateQueryKeys = {
  summary: ['integrations', 'docker', 'image-updates-summary'] as const,
  detail: (containerName: string) =>
    ['integrations', 'docker', 'containers', containerName, 'image-update'] as const,
}

const IMAGE_UPDATE_SUMMARY_REFETCH_INTERVAL_MS = 30_000
const IMAGE_UPDATE_DETAIL_REFETCH_INTERVAL_MS = 30_000

export type ImageUpdateSummaryEntry = {
  container_name: string
  available: boolean
  current_digest?: string | null
  latest_digest?: string | null
  last_checked_at?: string | null
  check_failed_at?: string | null
  check_error_reason?: string | null
}

export type ImageUpdateSummary = {
  byContainer: Record<string, ImageUpdateSummaryEntry>
  rateLimitSkippedCount: number
  rateLimitRemainingByRegistry: Record<string, number>
}

export function useImageUpdatesSummary() {
  return useQuery({
    queryKey: dockerImageUpdateQueryKeys.summary,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/image-updates/summary', {})
      const payload = unwrap<ImageUpdateSummaryResponse>(result)
      const byContainer: Record<string, ImageUpdateSummaryEntry> = {}
      for (const entry of payload.summaries) {
        byContainer[entry.container_name] = {
          container_name: entry.container_name,
          available: entry.available,
          current_digest: entry.current_digest ?? null,
          latest_digest: entry.latest_digest ?? null,
          last_checked_at: entry.last_checked_at ?? null,
          check_failed_at: entry.check_failed_at ?? null,
          check_error_reason: entry.check_error_reason ?? null,
        }
      }
      return {
        byContainer,
        rateLimitSkippedCount: payload.rate_limit_skipped_count,
        rateLimitRemainingByRegistry: payload.rate_limit_remaining_by_registry,
      }
    },
    refetchInterval: IMAGE_UPDATE_SUMMARY_REFETCH_INTERVAL_MS,
    staleTime: 5000,
  })
}

export function useImageUpdate(containerName: string): UseQueryResult<ImageUpdateDetail, ApiError> {
  return useQuery({
    queryKey: dockerImageUpdateQueryKeys.detail(containerName),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/integrations/docker/containers/{name}/image-update',
        { params: { path: { name: containerName } } },
      )
      return unwrap<ImageUpdateDetail>(result)
    },
    refetchInterval: IMAGE_UPDATE_DETAIL_REFETCH_INTERVAL_MS,
    enabled: containerName.length > 0,
  })
}
