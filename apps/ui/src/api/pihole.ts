import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import { apiClient, ApiError, unwrap } from './client'
import type { Schema } from './types'

type PiholeOverviewResponse = Schema<'PiholeOverviewResponse'>
type PiholeAdlistsResponse = Schema<'PiholeAdlistsResponse'>
type PiholeMessagesResponse = Schema<'PiholeMessagesResponse'>
type PiholeUpstreamsResponse = Schema<'PiholeUpstreamsResponse'>
type PiholeUnboundResponse = Schema<'PiholeUnboundResponse'>
type PiholeClientsResponse = Schema<'PiholeClientsResponse'>
type PiholeRecentBlockedResponse = Schema<'PiholeRecentBlockedResponse'>
type BlockingRequest = Schema<'BlockingRequest'>
type BlockingResponse = Schema<'BlockingResponse'>
type GravityUpdateRequest = Schema<'GravityUpdateRequest'>
type GravityUpdateResponse = Schema<'GravityUpdateResponse'>

export const piholeQueryKeys = {
  overview: ['integrations', 'pihole', 'overview'] as const,
  adlists: ['integrations', 'pihole', 'adlists'] as const,
  messages: ['integrations', 'pihole', 'messages'] as const,
  upstreams: ['integrations', 'pihole', 'upstreams'] as const,
  unbound: ['integrations', 'pihole', 'unbound'] as const,
  clients: (blocked: boolean, count: number) =>
    ['integrations', 'pihole', 'clients', blocked, count] as const,
  recentBlocked: (count: number) => ['integrations', 'pihole', 'recent-blocked', count] as const,
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

export function useAdlists(): UseQueryResult<PiholeAdlistsResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.adlists,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/adlists', {})
      return unwrap<PiholeAdlistsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useMessages(): UseQueryResult<PiholeMessagesResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.messages,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/messages', {})
      return unwrap<PiholeMessagesResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUpstreams(): UseQueryResult<PiholeUpstreamsResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.upstreams,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/upstreams', {})
      return unwrap<PiholeUpstreamsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useUnbound(): UseQueryResult<PiholeUnboundResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.unbound,
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/unbound', {})
      return unwrap<PiholeUnboundResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

const DEFAULT_TOP_COUNT = 10

export function useClients(
  blocked: boolean,
  count: number = DEFAULT_TOP_COUNT,
): UseQueryResult<PiholeClientsResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.clients(blocked, count),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/clients', {
        params: { query: { blocked, count } },
      })
      return unwrap<PiholeClientsResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

export function useRecentBlocked(
  count: number = DEFAULT_TOP_COUNT,
): UseQueryResult<PiholeRecentBlockedResponse, ApiError> {
  return useQuery({
    queryKey: piholeQueryKeys.recentBlocked(count),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/pihole/recent-blocked', {
        params: { query: { count } },
      })
      return unwrap<PiholeRecentBlockedResponse>(result)
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  })
}

/**
 * STAGE-006-022 — blocking enable/disable mutation.
 * POSTs the typed-phrase confirm to /api/integrations/pihole/blocking.
 * On success, invalidates the overview query so the new state shows after refetch.
 */
export function useBlockingMutation(): UseMutationResult<
  BlockingResponse,
  ApiError,
  BlockingRequest
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (variables: BlockingRequest) => {
      const result = await apiClient.POST('/api/integrations/pihole/blocking', {
        body: variables,
      })
      return unwrap<BlockingResponse>(result)
    },
    onSuccess: (data) => {
      // The POST response carries Pi-hole's authoritative new blocking state.
      // /overview is VM-sourced (homelab_pihole_blocking_enabled), only refreshed by
      // the pihole_blocking collector every ~30s, so refetching alone re-reads the
      // stale pre-write metric. Patch the cache immediately from the response, then
      // invalidate to reconcile once the collector catches up.
      queryClient.setQueryData<PiholeOverviewResponse>(piholeQueryKeys.overview, (prev) =>
        prev === undefined
          ? prev
          : {
              ...prev,
              // Fail-closed, matching the collector: only "enabled" => true.
              blocking_enabled: data.blocking === 'enabled',
              blocking_timer_seconds: data.timer,
            },
      )
      // Mark stale WITHOUT an immediate refetch. A refetch here would re-read the
      // stale VM metric (the pihole_blocking collector hasn't re-scraped yet) and
      // clobber the optimistic patch above. The 30s refetchInterval reconciles
      // once the collector catches up.
      void queryClient.invalidateQueries({
        queryKey: piholeQueryKeys.overview,
        refetchType: 'none',
      })
    },
  })
}

/**
 * STAGE-006-022 — gravity update mutation (slow, ~120s).
 * POSTs the typed-phrase confirm to /api/integrations/pihole/gravity/update.
 * On success, invalidates adlists + overview.
 */
export function useGravityUpdateMutation(): UseMutationResult<
  GravityUpdateResponse,
  ApiError,
  GravityUpdateRequest
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (variables: GravityUpdateRequest) => {
      const result = await apiClient.POST('/api/integrations/pihole/gravity/update', {
        body: variables,
      })
      return unwrap<GravityUpdateResponse>(result)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: piholeQueryKeys.adlists })
      void queryClient.invalidateQueries({ queryKey: piholeQueryKeys.overview })
    },
  })
}
