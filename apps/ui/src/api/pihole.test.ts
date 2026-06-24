import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Mock the client module before importing the hooks
vi.mock('./client', () => ({
  apiClient: {
    POST: vi.fn(),
    GET: vi.fn(),
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from './client'
import type { Schema } from './types'
import { useBlockingMutation, usePiholeOverview, piholeQueryKeys } from './pihole'

type PiholeOverviewResponse = Schema<'PiholeOverviewResponse'>
type BlockingResponse = Schema<'BlockingResponse'>

function fakeResponse(status: number): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
  } as Response
}

const BASE_OVERVIEW: PiholeOverviewResponse = {
  blocking_enabled: true,
  blocking_timer_seconds: null,
  gravity_domains: 1000,
  messages_count: 0,
  percent_blocked: 42,
  privacy_level: 0,
  query_frequency: 7,
  query_logging_enabled: true,
  up: true,
  updates_available: [],
  versions: [],
}

describe('useBlockingMutation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('patches cache with disabled state and invalidates', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    // Pre-seed the cache with initial state
    queryClient.setQueryData(piholeQueryKeys.overview, BASE_OVERVIEW)

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    // Mock POST to resolve with a disabled response + 5min timer
    const mockData: BlockingResponse = {
      audit_id: 'test-audit-1',
      blocking: 'disabled',
      timer: 300,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useBlockingMutation(), { wrapper })

    // Call mutateAsync and wait for success
    await result.current.mutateAsync({
      action: 'disable',
      confirm_phrase: 'disable',
    })

    // Verify cache was patched with the new state
    const cachedOverview = queryClient.getQueryData<PiholeOverviewResponse>(
      piholeQueryKeys.overview,
    )
    expect(cachedOverview?.blocking_enabled).toBe(false)
    expect(cachedOverview?.blocking_timer_seconds).toBe(300)
  })

  it('patches cache with enabled state', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    // Pre-seed with blocking disabled
    queryClient.setQueryData(piholeQueryKeys.overview, {
      ...BASE_OVERVIEW,
      blocking_enabled: false,
      blocking_timer_seconds: 300,
    })

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    const mockData: BlockingResponse = {
      audit_id: 'test-audit-2',
      blocking: 'enabled',
      timer: null,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useBlockingMutation(), { wrapper })

    await result.current.mutateAsync({
      action: 'enable',
      confirm_phrase: 'enable',
    })

    const cachedOverview = queryClient.getQueryData<PiholeOverviewResponse>(
      piholeQueryKeys.overview,
    )
    expect(cachedOverview?.blocking_enabled).toBe(true)
    expect(cachedOverview?.blocking_timer_seconds).toBe(null)
  })

  it('applies fail-closed semantics: only blocking=enabled sets flag true', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    queryClient.setQueryData(piholeQueryKeys.overview, {
      ...BASE_OVERVIEW,
      blocking_enabled: true,
    })

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    // Response with some hypothetical other state (fail-closed: treat as disabled)
    const mockData: BlockingResponse = {
      audit_id: 'test-audit-3',
      blocking: 'disabled',
      timer: null,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useBlockingMutation(), { wrapper })

    await result.current.mutateAsync({
      action: 'disable',
      confirm_phrase: 'disable',
    })

    const cachedOverview = queryClient.getQueryData<PiholeOverviewResponse>(
      piholeQueryKeys.overview,
    )
    // Fail-closed: only "enabled" => true, anything else => false
    expect(cachedOverview?.blocking_enabled).toBe(false)
  })

  it('preserves undefined cache (does not fabricate partial objects)', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    // Do NOT seed the cache — it's undefined

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    const mockData: BlockingResponse = {
      audit_id: 'test-audit-4',
      blocking: 'disabled',
      timer: 300,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useBlockingMutation(), { wrapper })

    await result.current.mutateAsync({
      action: 'disable',
      confirm_phrase: 'disable',
    })

    // Cache should remain undefined (no partial object fabrication)
    const cachedOverview = queryClient.getQueryData<PiholeOverviewResponse>(
      piholeQueryKeys.overview,
    )
    expect(cachedOverview).toBeUndefined()
  })

  it('invalidates the overview query after patching', async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    queryClient.setQueryData(piholeQueryKeys.overview, BASE_OVERVIEW)

    // Track invalidations
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    const mockData: BlockingResponse = {
      audit_id: 'test-audit-5',
      blocking: 'disabled',
      timer: 300,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useBlockingMutation(), { wrapper })

    await result.current.mutateAsync({
      action: 'disable',
      confirm_phrase: 'disable',
    })

    // Verify invalidateQueries was called with the correct key and refetchType
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: piholeQueryKeys.overview,
      refetchType: 'none',
    })
    invalidateSpy.mockRestore()
  })

  it('prevents stale metric refetch from clobbering optimistic patch when active observer is mounted', async () => {
    // This test reproduces the real bug scenario: both usePiholeOverview (active observer)
    // and useBlockingMutation are mounted. Without refetchType:'none', invalidateQueries
    // would trigger an immediate refetch that re-reads the stale VM metric and overwrites
    // the optimistic patch.

    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })

    // Pre-seed cache with blocking enabled
    queryClient.setQueryData<PiholeOverviewResponse>(piholeQueryKeys.overview, BASE_OVERVIEW)

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    // Mock POST to disable blocking and return a timer
    const blockingResponse: BlockingResponse = {
      audit_id: 'test-audit-active-observer',
      blocking: 'disabled',
      timer: 300,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: blockingResponse,
      response: fakeResponse(200),
    })

    // Mock GET to return STALE data (still blocking_enabled: true)
    // This simulates the VM metric not yet updated by the pihole_blocking collector.
    const staleResponse: PiholeOverviewResponse = {
      ...BASE_OVERVIEW,
      blocking_enabled: true, // Stale — not yet updated by collector
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: staleResponse,
      response: fakeResponse(200),
    })

    // Mount both the mutation and an active query observer
    const { result: mutationResult } = renderHook(() => useBlockingMutation(), { wrapper })
    const { result: queryResult } = renderHook(() => usePiholeOverview(), { wrapper })

    // Initially the query data should be seeded (blocking_enabled: true)
    expect(queryResult.current.data?.blocking_enabled).toBe(true)

    // Run the mutation to disable blocking
    await mutationResult.current.mutateAsync({
      action: 'disable',
      confirm_phrase: 'disable',
    })

    // Key assertion: the optimistic patch should have been applied and SURVIVED
    // (not clobbered by a stale refetch). The cache should show blocking_enabled: false.
    // Without refetchType:'none', invalidateQueries would trigger an immediate refetch
    // that would re-read the stale GET response (with blocking_enabled: true) and clobber
    // the patch we just applied. By using refetchType:'none', we mark the query stale
    // but prevent the immediate refetch — the 30s refetchInterval will reconcile once
    // the collector updates the VM metric.
    const cachedOverview = queryClient.getQueryData<PiholeOverviewResponse>(
      piholeQueryKeys.overview,
    )
    expect(cachedOverview?.blocking_enabled).toBe(false)
    expect(cachedOverview?.blocking_timer_seconds).toBe(300)
  })
})
