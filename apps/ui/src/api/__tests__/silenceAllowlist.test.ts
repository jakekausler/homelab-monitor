import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', () => ({
  apiClient: {
    GET: vi.fn(),
    POST: vi.fn(),
    DELETE: vi.fn(),
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from '../client'
import {
  silenceAllowlistKeys,
  useSilenceAllowlist,
  useCreateSilenceAllowlistEntry,
  useDeleteSilenceAllowlistEntry,
  type SilenceAllowlistListResponse,
  type SilenceAllowlistResponse,
} from '../silenceAllowlist'

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

describe('silenceAllowlistKeys', () => {
  it('all returns the base key', () => {
    expect(silenceAllowlistKeys.all).toEqual(['silence-allowlist'])
  })

  it('list returns the list key', () => {
    expect(silenceAllowlistKeys.list()).toEqual(['silence-allowlist', 'list'])
  })
})

describe('useSilenceAllowlist', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetches from the correct endpoint', async () => {
    const mockData: SilenceAllowlistListResponse = { entries: [] }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSilenceAllowlist(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/silence-allowlist')
    expect(result.current.data).toEqual(mockData)
  })

  it('returns entries in response', async () => {
    const mockData: SilenceAllowlistListResponse = {
      entries: [
        {
          id: 1,
          template_hash: 'h1',
          service_key: 'svc1',
          schedule_kind: 'always',
          schedule_value: '',
          reason: 'test',
          created_at: '2026-01-01T00:00:00+00:00',
          expires_at: null,
        },
      ],
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSilenceAllowlist(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.entries).toHaveLength(1)
    expect(result.current.data?.entries?.[0]?.id).toBe(1)
  })
})

describe('useCreateSilenceAllowlistEntry', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST with correct endpoint and body', async () => {
    const mockData: SilenceAllowlistResponse = {
      id: 1,
      template_hash: 'h1',
      service_key: 'svc1',
      schedule_kind: 'always',
      schedule_value: '',
      reason: 'test',
      created_at: '2026-01-01T00:00:00+00:00',
      expires_at: null,
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(201),
    })

    const { result } = renderHook(() => useCreateSilenceAllowlistEntry(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      service_key: 'svc1',
      schedule_kind: 'always',
      schedule_value: '',
      reason: 'test',
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith('/api/logs/signatures/silence-allowlist', {
      body: {
        service_key: 'svc1',
        schedule_kind: 'always',
        schedule_value: '',
        reason: 'test',
      },
    })
    expect(result.current.data).toEqual(mockData)
  })
})

describe('useDeleteSilenceAllowlistEntry', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls DELETE with correct path param and handles 204', async () => {
    vi.mocked(apiClient.DELETE).mockResolvedValue({
      response: fakeResponse(204),
    } as unknown as Awaited<ReturnType<typeof apiClient.DELETE>>)

    const { result } = renderHook(() => useDeleteSilenceAllowlistEntry(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate(42)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.DELETE).toHaveBeenCalledWith(
      '/api/logs/signatures/silence-allowlist/{entry_id}',
      {
        params: {
          path: {
            entry_id: 42,
          },
        },
      },
    )
  })
})
