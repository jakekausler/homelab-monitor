import {
  useInfiniteQuery,
  useQuery,
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
