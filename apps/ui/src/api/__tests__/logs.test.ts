// Project test conventions (reused from docker.test.ts):
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom
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
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from '../client'
import { identitiesToServicesCsv, logsQueryKeys, useLogsQuery, useLogsServicesQuery } from '../logs'

// ---------------------------------------------------------------------------
// Helpers
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

// ---------------------------------------------------------------------------
// identitiesToServicesCsv
// ---------------------------------------------------------------------------

describe('identitiesToServicesCsv', () => {
  it('returns empty string for empty array', () => {
    expect(identitiesToServicesCsv([])).toBe('')
  })

  it('formats a single identity as source_type:service', () => {
    expect(identitiesToServicesCsv([{ service: 'nginx', source_type: 'docker' }])).toBe(
      'docker:nginx',
    )
  })

  it('joins multiple identities with commas in order', () => {
    expect(
      identitiesToServicesCsv([
        { service: 'nginx', source_type: 'docker' },
        { service: 'hmrun', source_type: 'cron' },
      ]),
    ).toBe('docker:nginx,cron:hmrun')
  })

  it('preserves colons in service names verbatim', () => {
    expect(identitiesToServicesCsv([{ service: 'a:b', source_type: 'docker' }])).toBe('docker:a:b')
  })
})

// ---------------------------------------------------------------------------
// logsQueryKeys
// ---------------------------------------------------------------------------

describe('logsQueryKeys', () => {
  it('query() returns expected array shape', () => {
    expect(logsQueryKeys.query('expr', 's', 'e', 'svc')).toEqual([
      'logs',
      'query',
      'expr',
      's',
      'e',
      'svc',
    ])
  })

  it('services() returns expected array shape', () => {
    expect(logsQueryKeys.services('s', 'e', 100)).toEqual(['logs', 'services', 's', 'e', 100])
  })

  it('query() produces different keys for different services', () => {
    const key1 = logsQueryKeys.query('expr', 's', 'e', 'docker:nginx')
    const key2 = logsQueryKeys.query('expr', 's', 'e', 'cron:hmrun')
    expect(key1).not.toEqual(key2)
  })
})

// ---------------------------------------------------------------------------
// useLogsQuery
// ---------------------------------------------------------------------------

describe('useLogsQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when expr is empty', () => {
    const { result } = renderHook(
      () => useLogsQuery('', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z'),
      {
        wrapper: makeWrapper(),
      },
    )
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('is disabled when start is empty', () => {
    const { result } = renderHook(() => useLogsQuery('error', '', '2024-01-02T00:00:00Z'), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
  })

  it('is disabled when end is empty', () => {
    const { result } = renderHook(() => useLogsQuery('error', '2024-01-01T00:00:00Z', ''), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
  })

  it('fetches when expr, start, and end are non-empty', async () => {
    const mockData = { entries: [], next_cursor: null }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () => useLogsQuery('error', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z', 'docker:nginx'),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/query', {
      params: {
        query: {
          expr: 'error',
          start: '2024-01-01T00:00:00Z',
          end: '2024-01-02T00:00:00Z',
          services: 'docker:nginx',
        },
      },
    })
  })

  it('omits services param when services is empty string', async () => {
    const mockData = { entries: [], next_cursor: null }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () => useLogsQuery('error', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z'),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/query', {
      params: {
        query: {
          expr: 'error',
          start: '2024-01-01T00:00:00Z',
          end: '2024-01-02T00:00:00Z',
        },
      },
    })
  })

  it('query key includes services so different selections refetch separately', () => {
    const key1 = logsQueryKeys.query('e', 's', 'end', 'docker:nginx')
    const key2 = logsQueryKeys.query('e', 's', 'end', 'cron:hmrun')
    expect(key1).not.toEqual(key2)
  })
})

// ---------------------------------------------------------------------------
// useLogsServicesQuery
// ---------------------------------------------------------------------------

describe('useLogsServicesQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when start is empty', () => {
    const { result } = renderHook(() => useLogsServicesQuery('', '2024-01-02T00:00:00Z'), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('is disabled when end is empty', () => {
    const { result } = renderHook(() => useLogsServicesQuery('2024-01-01T00:00:00Z', ''), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
  })

  it('fetches when start and end are non-empty', async () => {
    const mockData = { services: [] }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () => useLogsServicesQuery('2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z', 50),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/services', {
      params: {
        query: {
          start: '2024-01-01T00:00:00Z',
          end: '2024-01-02T00:00:00Z',
          limit: 50,
        },
      },
    })
  })

  it('uses default limit of 100 when limit is omitted', async () => {
    const mockData = { services: [] }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () => useLogsServicesQuery('2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z'),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/services', {
      params: {
        query: {
          start: '2024-01-01T00:00:00Z',
          end: '2024-01-02T00:00:00Z',
          limit: 100,
        },
      },
    })
  })
})
