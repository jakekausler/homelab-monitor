// Project test conventions:
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom (default for ui package)
// - Mocking: vi.mock() factory at top, vi.mocked() for typed access
// - Async: async/await + waitFor from @testing-library/react
// - Wrapper: makeWrapper() creates QueryClientProvider with retry:false

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', () => ({
  apiClient: {
    GET: vi.fn(),
    POST: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
    PUT: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number
    code: string
    constructor(opts: { status: number; code: string; message: string }) {
      super(opts.message)
      this.status = opts.status
      this.code = opts.code
    }
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from '../client'
import { useLogsRetention, useUpdateLogsRetention, settingsLogsKeys } from '@/api/settingsLogs'
import type { Schema } from '@/api/types'

type LogsRetentionResponse = Schema<'LogsRetentionResponse'>

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function fakeResponse(status: number): Response {
  return new Response(null, { status })
}

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const MOCK_RETENTION: LogsRetentionResponse = {
  retention_days: 30,
  pending_retention_days: null,
  disk_used_gb: 1.25,
  disk_used_pct: 12.5,
  disk_budget_available: true,
  warn_pct: 70,
  crit_pct: 85,
  retention_source: 'default',
  restart_required: false,
}

// ---------------------------------------------------------------------------
// settingsLogsKeys
// ---------------------------------------------------------------------------

describe('settingsLogsKeys', () => {
  it('all returns expected array', () => {
    expect(settingsLogsKeys.all).toEqual(['settings-logs'])
  })

  it('retention() returns expected array', () => {
    expect(settingsLogsKeys.retention()).toEqual(['settings-logs', 'retention'])
  })
})

// ---------------------------------------------------------------------------
// useLogsRetention
// ---------------------------------------------------------------------------

describe('useLogsRetention', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetches retention settings and returns unwrapped data', async () => {
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: MOCK_RETENTION,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useLogsRetention(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/settings/logs/retention', {})
    expect(result.current.data).toEqual(MOCK_RETENTION)
  })

  it('surfaces error when API returns error response', async () => {
    vi.mocked(apiClient.GET).mockRejectedValue(new Error('server_error'))

    const { result } = renderHook(() => useLogsRetention(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toBeInstanceOf(Error)
  })
})

// ---------------------------------------------------------------------------
// useUpdateLogsRetention
// ---------------------------------------------------------------------------

describe('useUpdateLogsRetention', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls PATCH /api/settings/logs/retention with the body and returns updated data', async () => {
    const updated: LogsRetentionResponse = {
      ...MOCK_RETENTION,
      retention_days: 14,
      pending_retention_days: 14,
      restart_required: true,
      disk_budget_available: true,
    }
    vi.mocked(apiClient.PATCH).mockResolvedValue({
      data: updated,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useUpdateLogsRetention(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ retention_days: 14 })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.PATCH).toHaveBeenCalledWith('/api/settings/logs/retention', {
      body: { retention_days: 14 },
    })
    expect(result.current.data).toEqual(updated)
  })

  it('invalidates settings-logs query cache on PATCH success', async () => {
    const updated: LogsRetentionResponse = {
      ...MOCK_RETENTION,
      retention_days: 90,
      pending_retention_days: 90,
      restart_required: true,
      disk_budget_available: true,
    }
    vi.mocked(apiClient.PATCH).mockResolvedValue({
      data: updated,
      response: fakeResponse(200),
    })

    // Create a QueryClient we can spy on before the hook renders
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const wrapper = ({ children }: { children: ReactNode }) =>
      React.createElement(QueryClientProvider, { client: queryClient }, children)

    const { result } = renderHook(() => useUpdateLogsRetention(), { wrapper })

    result.current.mutate({ retention_days: 90 })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['settings-logs'] })
  })

  it('surfaces error when PATCH fails', async () => {
    vi.mocked(apiClient.PATCH).mockRejectedValue(new Error('validation_error'))

    const { result } = renderHook(() => useUpdateLogsRetention(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ retention_days: 7 })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})
