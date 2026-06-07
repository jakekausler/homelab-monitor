// Project test conventions:
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom
// - Mocking: vi.mock('../client') factory at top
// - Async: async/await + waitFor from @testing-library/react

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', () => ({
  apiClient: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient, unwrap } from '../client'
import { useContainerCrashes, useContainerCrashDetail } from '../docker'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
// useContainerCrashes
// ---------------------------------------------------------------------------

describe('useContainerCrashes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('returns data on successful GET', async () => {
    const mockData = { container_name: 'c', crashes: [] }
    vi.mocked(apiClient.GET).mockResolvedValue({ data: mockData, response: new Response() })
    vi.mocked(unwrap).mockReturnValue(mockData)

    const { result } = renderHook(() => useContainerCrashes('c'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(result.current.data).toEqual(mockData)
    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/integrations/docker/containers/{name}/crashes',
      { params: { path: { name: 'c' } } },
    )
  })

  it('stays idle when containerName is empty', () => {
    const { result } = renderHook(() => useContainerCrashes(''), {
      wrapper: makeWrapper(),
    })

    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// useContainerCrashDetail
// ---------------------------------------------------------------------------

describe('useContainerCrashDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('returns detail data when enabled=true', async () => {
    const mockDetail = {
      crash_id: 'x',
      container_name: 'c',
      exit_code: 1,
      finished_at: '2026-06-07T00:00:00Z',
      image_name: null,
      compose_project: null,
      compose_service: null,
      line_count: 1,
      truncated: false,
      degraded: false,
      created_at: '2026-06-07T00:00:01Z',
      window_start: '2026-06-07T00:00:00Z',
      window_end: '2026-06-07T00:01:00Z',
      lines: [],
    }
    vi.mocked(apiClient.GET).mockResolvedValue({ data: mockDetail, response: new Response() })
    vi.mocked(unwrap).mockReturnValue(mockDetail)

    const { result } = renderHook(() => useContainerCrashDetail('c', 'x', true), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(result.current.data).toEqual(mockDetail)
    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/integrations/docker/containers/{name}/crashes/{crash_id}',
      { params: { path: { name: 'c', crash_id: 'x' } } },
    )
  })

  it('stays idle when enabled=false', () => {
    const { result } = renderHook(() => useContainerCrashDetail('c', 'x', false), {
      wrapper: makeWrapper(),
    })

    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })
})
