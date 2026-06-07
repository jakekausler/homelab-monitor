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
type SuggestionAcceptRequest = Schema<'SuggestionAcceptRequest'>
type SuggestionAcceptResponse = Schema<'SuggestionAcceptResponse'>
type SuggestionCustomizeRequest = Schema<'SuggestionCustomizeRequest'>
type SuggestionCustomizeResponse = Schema<'SuggestionCustomizeResponse'>
type SuggestionIgnoreResponse = Schema<'SuggestionIgnoreResponse'>

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
type CreateProbeTargetRequest = Schema<'CreateProbeTargetRequest'>
type UpdateProbeTargetRequest = Schema<'UpdateProbeTargetRequest'>
type ProbeSummaryResponse = Schema<'ProbeSummaryResponse'>
type ImageUpdateSummaryResponse = Schema<'ImageUpdateSummaryResponse'>
type ImageUpdateDetail = Schema<'ImageUpdateDetail'>
type PullAndRestartRequest = Schema<'PullAndRestartRequest'>
type PullAndRestartAcceptedResponse = Schema<'PullAndRestartAcceptedResponse'>
type ComposeActionDetailResponse = Schema<'ComposeActionDetailResponse'>
type ComposeActionListResponse = Schema<'ComposeActionListResponse'>

/**
 * ORPHANED by STAGE-003-012 Refinement scope expansion (2026-05-26):
 * The new "Probes" panel (per-container cards) does not wire Accept/
 * Customize/Ignore. These hooks remain for backend coverage and will be
 * subsumed by EPIC-011's global Discovery & Suggestions inbox. Do not
 * delete; do not call from new UI code without checking with EPIC-011 design.
 *
 * See:
 *   - epics/EPIC-011-discovery-suggestions/EPIC-011.md "Inherited carry-forwards from EPIC-003"
 *   - epics/EPIC-003-docker/EPIC-003.md "Cross-epic carry-forward → EPIC-011"
 *
 * The suggestions schema is stable; only hook locations and API URL paths may change.
 */

export function useAcceptDockerSuggestion() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { suggestionId: string; applyDefaultProbes?: boolean }) => {
      const result = await apiClient.POST(
        '/api/integrations/docker/suggestions/{suggestion_id}/accept',
        {
          params: { path: { suggestion_id: vars.suggestionId } },
          body: {
            apply_default_probes: vars.applyDefaultProbes ?? true,
          } satisfies SuggestionAcceptRequest,
        },
      )
      return unwrap<SuggestionAcceptResponse>(result)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('pending') })
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('accepted') })
      void queryClient.invalidateQueries({ queryKey: dockerProbeQueryKeys.summary })
    },
  })
}

export function useCustomizeDockerSuggestion() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: {
      suggestionId: string
      probes: SuggestionCustomizeRequest['probes']
    }) => {
      const result = await apiClient.POST(
        '/api/integrations/docker/suggestions/{suggestion_id}/customize',
        {
          params: { path: { suggestion_id: vars.suggestionId } },
          body: { probes: vars.probes } satisfies SuggestionCustomizeRequest,
        },
      )
      return unwrap<SuggestionCustomizeResponse>(result)
    },
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('pending') })
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('accepted') })
      void queryClient.invalidateQueries({ queryKey: dockerProbeQueryKeys.summary })
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.list(data.suggestion.container_name),
      })
    },
  })
}

export function useIgnoreDockerSuggestion() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { suggestionId: string }) => {
      const result = await apiClient.POST(
        '/api/integrations/docker/suggestions/{suggestion_id}/ignore',
        {
          params: { path: { suggestion_id: vars.suggestionId } },
        },
      )
      return unwrap<SuggestionIgnoreResponse>(result)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('pending') })
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('ignored') })
      void queryClient.invalidateQueries({ queryKey: dockerQueryKeys.suggestions('accepted') })
    },
  })
}

// --- Default-probes preview ---
type SuggestionDefaultProbesResponse = Schema<'SuggestionDefaultProbesResponse'>

export const dockerSuggestionDefaultProbesQueryKeys = {
  detail: (suggestionId: string) =>
    ['integrations', 'docker', 'suggestions', suggestionId, 'default-probes'] as const,
}

export function useSuggestionDefaultProbes(
  suggestionId: string,
): UseQueryResult<SuggestionDefaultProbesResponse, ApiError> {
  return useQuery({
    queryKey: dockerSuggestionDefaultProbesQueryKeys.detail(suggestionId),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/integrations/docker/suggestions/{suggestion_id}/default-probes',
        { params: { path: { suggestion_id: suggestionId } } },
      )
      return unwrap<SuggestionDefaultProbesResponse>(result)
    },
    enabled: suggestionId.length > 0,
    staleTime: 30_000,
  })
}

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

export const dockerComposeActionQueryKeys = {
  detail: (actionId: number) => ['integrations', 'docker', 'compose-actions', actionId] as const,
  list: (containerName: string, limit: number) =>
    ['integrations', 'docker', 'compose-actions', 'list', containerName, limit] as const,
  // Prefix used to invalidate all `list(...)` queries for a container regardless of limit.
  listAllForContainer: (containerName: string) =>
    ['integrations', 'docker', 'compose-actions', 'list', containerName] as const,
}

const COMPOSE_ACTION_POLL_INTERVAL_MS = 2000
const COMPOSE_ACTION_LIST_REFETCH_MS = 5000
export const COMPOSE_ACTIVE_STATES = new Set(['pulling', 'building', 'restarting', 'running'])

export type ImageUpdateSummaryEntry = {
  container_name: string
  available: boolean
  source: 'registry' | 'local_build'
  current_digest?: string | null
  latest_digest?: string | null
  last_checked_at?: string | null
  check_failed_at?: string | null
  check_error_reason?: string | null
  compose_service?: string | null
  build_context_path?: string | null
  last_source_hash?: string | null
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
        const source = String(entry.source) === 'local_build' ? 'local_build' : 'registry'
        const mapped: ImageUpdateSummaryEntry = {
          container_name: String(entry.container_name),
          available: Boolean(entry.available),
          source,
          current_digest: entry.current_digest ? String(entry.current_digest) : null,
          latest_digest: entry.latest_digest ? String(entry.latest_digest) : null,
          last_checked_at: entry.last_checked_at ? String(entry.last_checked_at) : null,
          check_failed_at: entry.check_failed_at ? String(entry.check_failed_at) : null,
          check_error_reason: entry.check_error_reason ? String(entry.check_error_reason) : null,
          compose_service: entry.compose_service ? String(entry.compose_service) : null,
          build_context_path: entry.build_context_path ? String(entry.build_context_path) : null,
          last_source_hash: entry.last_source_hash ? String(entry.last_source_hash) : null,
        }
        byContainer[mapped.container_name] = mapped
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

/**
 * STAGE-003-010 — Pull & Restart mutation.
 * POSTs the typed-phrase confirm to /api/.../pull-and-restart.
 * Returns { action_id, state } on success; throws ApiError on 400/401/403/404.
 */
export function useStartPullAndRestart(containerName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (variables: { confirmPhrase: string }) => {
      const result = await apiClient.POST(
        '/api/integrations/docker/containers/{name}/pull-and-restart',
        {
          params: { path: { name: containerName } },
          body: { confirm_phrase: variables.confirmPhrase } satisfies PullAndRestartRequest,
        },
      )
      return unwrap<PullAndRestartAcceptedResponse>(result)
    },
    onSuccess: () => {
      // Use prefix key so all `list(...)` queries for this container invalidate
      // regardless of which `limit` they were instantiated with.
      void queryClient.invalidateQueries({
        queryKey: dockerComposeActionQueryKeys.listAllForContainer(containerName),
      })
    },
  })
}

/**
 * STAGE-003-010 — Get a single compose action; poll every 2s while
 * state === 'running'. Pass enabled=false to suspend.
 */
export function useGetComposeAction(
  actionId: number | null,
  opts?: { enabled?: boolean },
): UseQueryResult<ComposeActionDetailResponse, ApiError> {
  return useQuery({
    queryKey: dockerComposeActionQueryKeys.detail(actionId ?? -1),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/compose-actions/{action_id}', {
        params: { path: { action_id: actionId as number } },
      })
      return unwrap<ComposeActionDetailResponse>(result)
    },
    enabled: (opts?.enabled ?? true) && actionId !== null,
    refetchInterval: (query) => {
      const data = query.state.data
      return data && COMPOSE_ACTIVE_STATES.has(data.state) ? COMPOSE_ACTION_POLL_INTERVAL_MS : false
    },
  })
}

/**
 * STAGE-003-010 — List recent compose actions for one container.
 */
export function useListComposeActions(
  containerName: string,
  limit = 10,
): UseQueryResult<ComposeActionListResponse, ApiError> {
  return useQuery({
    queryKey: dockerComposeActionQueryKeys.list(containerName, limit),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/compose-actions', {
        params: { query: { container: containerName, limit } },
      })
      return unwrap<ComposeActionListResponse>(result)
    },
    enabled: containerName.length > 0,
    refetchInterval: COMPOSE_ACTION_LIST_REFETCH_MS,
  })
}

// ---------------------------------------------------------------------------
// STAGE-003-012 — probe-target CRUD with optimistic updates
// ---------------------------------------------------------------------------

/**
 * STAGE-003-012 — Optimistic create of a probe target.
 *
 * Cache key:  dockerProbeQueryKeys.list(containerName)
 * Shape:      { probes: ProbeRow[] }
 * Strategy:
 *   onMutate    — snapshot, write cache with optimistic row appended
 *   onError     — rollback to snapshot
 *   onSettled   — invalidate to refetch authoritative state
 *
 * Required: containerName is passed in body since the POST endpoint
 * is not scoped under a container path.
 */
export function useCreateProbeTarget() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { body: CreateProbeTargetRequest }) => {
      const result = await apiClient.POST('/api/integrations/docker/probe-targets', {
        body: vars.body,
      })
      return unwrap<ProbeRowSchema>(result)
    },
    onMutate: async (vars) => {
      const listKey = dockerProbeQueryKeys.list(vars.body.container_name)
      await queryClient.cancelQueries({ queryKey: listKey })
      const previous = queryClient.getQueryData<ListProbesResponse>(listKey)
      // Synthesize an optimistic row. id is a temporary client-side string;
      // onSettled refetch replaces with real id.
      const optimistic: ProbeRowSchema = {
        id: `optimistic-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        container_name: vars.body.container_name,
        kind: vars.body.kind,
        name: vars.body.name,
        target_value: vars.body.target_value,
        config_source: 'manual',
        enabled: true,
        interval_seconds: vars.body.interval_seconds ?? 60,
        timeout_seconds: vars.body.timeout_seconds ?? 10,
        last_run_at: null,
        last_status: null,
        last_error: null,
        created_at: new Date().toISOString(),
        hidden_at: null,
        exec_authorized: false,
      }
      if (previous) {
        queryClient.setQueryData<ListProbesResponse>(listKey, {
          ...previous,
          probes: [...previous.probes, optimistic],
        })
      } else {
        queryClient.setQueryData<ListProbesResponse>(listKey, { probes: [optimistic] })
      }
      return { previous, containerName: vars.body.container_name }
    },
    onError: (_err, _vars, context) => {
      if (context?.previous && context.containerName) {
        queryClient.setQueryData(dockerProbeQueryKeys.list(context.containerName), context.previous)
      }
    },
    onSettled: (_data, _err, vars) => {
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.list(vars.body.container_name),
      })
      void queryClient.invalidateQueries({ queryKey: dockerProbeQueryKeys.summary })
    },
  })
}

/**
 * STAGE-003-012 — Optimistic update of a probe target.
 *
 * Cache key:  dockerProbeQueryKeys.list(containerName)
 * Shape:      { probes: ProbeRow[] }
 * Strategy:
 *   onMutate    — snapshot, replace row in place with optimistic values
 *   onError     — rollback to snapshot
 *   onSettled   — invalidate to refetch authoritative state
 *
 * Required: containerName MUST be passed by the caller since the PATCH
 * endpoint takes only probe_id and we have no way to derive containerName
 * from the cache without scanning every list query.
 */
export function useUpdateProbeTarget() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: {
      probeId: string
      containerName: string
      body: UpdateProbeTargetRequest
    }) => {
      const result = await apiClient.PATCH('/api/integrations/docker/probe-targets/{probe_id}', {
        params: { path: { probe_id: vars.probeId } },
        body: vars.body,
      })
      return unwrap<ProbeRowSchema>(result)
    },
    onMutate: async (vars) => {
      const listKey = dockerProbeQueryKeys.list(vars.containerName)
      await queryClient.cancelQueries({ queryKey: listKey })
      const previous = queryClient.getQueryData<ListProbesResponse>(listKey)
      if (previous) {
        queryClient.setQueryData<ListProbesResponse>(listKey, {
          ...previous,
          probes: previous.probes.map((p) =>
            p.id === vars.probeId
              ? {
                  ...p,
                  target_value: vars.body.target_value ?? p.target_value,
                  interval_seconds: vars.body.interval_seconds ?? p.interval_seconds,
                  timeout_seconds: vars.body.timeout_seconds ?? p.timeout_seconds,
                }
              : p,
          ),
        })
      }
      return { previous }
    },
    onError: (_err, vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(dockerProbeQueryKeys.list(vars.containerName), context.previous)
      }
    },
    onSettled: (_data, _err, vars) => {
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.list(vars.containerName),
      })
      void queryClient.invalidateQueries({ queryKey: dockerProbeQueryKeys.summary })
    },
  })
}

/**
 * STAGE-003-012 — Optimistic delete of a probe target.
 *
 * Cache key:  dockerProbeQueryKeys.list(containerName)
 * Shape:      { probes: ProbeRow[] }
 * Strategy:
 *   onMutate    — snapshot, write filtered cache (row removed)
 *   onError     — rollback to snapshot
 *   onSettled   — invalidate to refetch authoritative state
 *
 * Required: containerName MUST be passed by the caller since the DELETE
 * endpoint takes only probe_id and we have no way to derive containerName
 * from the cache without scanning every list query.
 */
export function useDeleteProbeTarget() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { probeId: string; containerName: string }) => {
      const result = await apiClient.DELETE('/api/integrations/docker/probe-targets/{probe_id}', {
        params: { path: { probe_id: vars.probeId } },
      })
      // 204 returns no body; short-circuit before unwrap (matches useHideCron pattern in crons.ts).
      if (result.response.status === 204) return
      return unwrap<undefined>(result)
    },
    onMutate: async (vars) => {
      const listKey = dockerProbeQueryKeys.list(vars.containerName)
      await queryClient.cancelQueries({ queryKey: listKey })
      const previous = queryClient.getQueryData<ListProbesResponse>(listKey)
      if (previous) {
        queryClient.setQueryData<ListProbesResponse>(listKey, {
          ...previous,
          probes: previous.probes.filter((p) => p.id !== vars.probeId),
        })
      }
      return { previous }
    },
    onError: (_err, vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(dockerProbeQueryKeys.list(vars.containerName), context.previous)
      }
    },
    onSettled: (_data, _err, vars) => {
      void queryClient.invalidateQueries({
        queryKey: dockerProbeQueryKeys.list(vars.containerName),
      })
      void queryClient.invalidateQueries({ queryKey: dockerProbeQueryKeys.summary })
    },
  })
}

// ---------------------------------------------------------------------------
// STAGE-003-011 — per-container log viewer
// ---------------------------------------------------------------------------

type ContainerLogsResponse = Schema<'ContainerLogsResponse'>

/** STAGE-004-008 — either a preset duration token OR an explicit ISO window. */
export type ContainerLogsRange = { since: string } | { start: string; end: string }

function rangeKeyPart(range: ContainerLogsRange): string {
  return 'since' in range ? `since:${range.since}` : `range:${range.start}..${range.end}`
}

export const dockerLogsQueryKeys = {
  logs: (containerName: string, range: ContainerLogsRange) =>
    ['integrations', 'docker', 'containers', containerName, 'logs', rangeKeyPart(range)] as const,
}

/**
 * Fetch recent log lines for one container from VictoriaLogs.
 * Manual refresh only (no refetchInterval per D-MANUAL-REFRESH-V1).
 * STAGE-004-007: A1 cursor pagination.
 * STAGE-004-008: accepts a preset `since` OR an explicit ISO `start`/`end`.
 *
 * @param containerName — container name (route param)
 * @param range — `{ since }` preset token OR `{ start, end }` ISO window
 */
export function useContainerLogs(
  containerName: string,
  range: ContainerLogsRange,
): UseInfiniteQueryResult<
  { pages: ContainerLogsResponse[]; pageParams: (string | undefined)[] },
  ApiError
> {
  return useInfiniteQuery({
    queryKey: dockerLogsQueryKeys.logs(containerName, range),
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const query: Record<string, string> =
        'since' in range ? { since: range.since } : { start: range.start, end: range.end }
      if (pageParam) query.cursor = pageParam
      const result = await apiClient.GET('/api/integrations/docker/containers/{name}/logs', {
        params: { path: { name: containerName }, query },
      })
      return unwrap<ContainerLogsResponse>(result)
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: containerName.length > 0,
    retry: false,
  })
}

// ---------------------------------------------------------------------------
// STAGE-004-032 — container crash log correlation
// ---------------------------------------------------------------------------

type ContainerCrashesResponse = Schema<'ContainerCrashesResponse'>
type ContainerCrashDetail = Schema<'ContainerCrashDetail'>

export const dockerCrashesQueryKeys = {
  list: (containerName: string) =>
    ['integrations', 'docker', 'containers', containerName, 'crashes'] as const,
  detail: (containerName: string, crashId: string) =>
    ['integrations', 'docker', 'containers', containerName, 'crashes', crashId] as const,
}

const CRASHES_STALE_TIME_MS = 30_000

/**
 * STAGE-004-032 — list detected crashes for one container (summaries only).
 */
export function useContainerCrashes(
  containerName: string,
): UseQueryResult<ContainerCrashesResponse, ApiError> {
  return useQuery({
    queryKey: dockerCrashesQueryKeys.list(containerName),
    queryFn: async () => {
      const result = await apiClient.GET('/api/integrations/docker/containers/{name}/crashes', {
        params: { path: { name: containerName } },
      })
      return unwrap<ContainerCrashesResponse>(result)
    },
    enabled: containerName.length > 0,
    staleTime: CRASHES_STALE_TIME_MS,
    retry: false,
  })
}

/**
 * STAGE-004-032 — one crash's detail incl. the persisted VL log window.
 * Pass enabled=false to defer the fetch until the row is expanded.
 */
export function useContainerCrashDetail(
  containerName: string,
  crashId: string,
  enabled: boolean,
): UseQueryResult<ContainerCrashDetail, ApiError> {
  return useQuery({
    queryKey: dockerCrashesQueryKeys.detail(containerName, crashId),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/integrations/docker/containers/{name}/crashes/{crash_id}',
        { params: { path: { name: containerName, crash_id: crashId } } },
      )
      return unwrap<ContainerCrashDetail>(result)
    },
    enabled: enabled && containerName.length > 0 && crashId.length > 0,
    staleTime: CRASHES_STALE_TIME_MS,
    retry: false,
  })
}

// ---------------------------------------------------------------------------
// STAGE-004-033 — container healthcheck-incident log correlation
// ---------------------------------------------------------------------------

type ContainerHealthcheckIncidentsResponse = Schema<'ContainerHealthcheckIncidentsResponse'>
type ContainerHealthcheckIncidentDetail = Schema<'ContainerHealthcheckIncidentDetail'>

export const dockerHealthcheckQueryKeys = {
  list: (containerName: string) =>
    ['integrations', 'docker', 'containers', containerName, 'healthcheck-incidents'] as const,
  detail: (containerName: string, incidentId: string) =>
    [
      'integrations',
      'docker',
      'containers',
      containerName,
      'healthcheck-incidents',
      incidentId,
    ] as const,
}

const HEALTHCHECK_STALE_TIME_MS = 30_000

/**
 * STAGE-004-033 — list detected healthcheck incidents for one container (summaries only).
 */
export function useContainerHealthcheckIncidents(
  containerName: string,
): UseQueryResult<ContainerHealthcheckIncidentsResponse, ApiError> {
  return useQuery({
    queryKey: dockerHealthcheckQueryKeys.list(containerName),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/integrations/docker/containers/{name}/healthcheck-incidents',
        { params: { path: { name: containerName } } },
      )
      return unwrap<ContainerHealthcheckIncidentsResponse>(result)
    },
    enabled: containerName.length > 0,
    staleTime: HEALTHCHECK_STALE_TIME_MS,
    retry: false,
  })
}

/**
 * STAGE-004-033 — one incident's detail incl. the persisted VL log window.
 * Pass enabled=false to defer the fetch until the row is expanded.
 */
export function useContainerHealthcheckIncidentDetail(
  containerName: string,
  incidentId: string,
  enabled: boolean,
): UseQueryResult<ContainerHealthcheckIncidentDetail, ApiError> {
  return useQuery({
    queryKey: dockerHealthcheckQueryKeys.detail(containerName, incidentId),
    queryFn: async () => {
      const result = await apiClient.GET(
        '/api/integrations/docker/containers/{name}/healthcheck-incidents/{incident_id}',
        { params: { path: { name: containerName, incident_id: incidentId } } },
      )
      return unwrap<ContainerHealthcheckIncidentDetail>(result)
    },
    enabled: enabled && containerName.length > 0 && incidentId.length > 0,
    staleTime: HEALTHCHECK_STALE_TIME_MS,
    retry: false,
  })
}
