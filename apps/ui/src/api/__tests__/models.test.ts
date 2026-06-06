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

import { apiClient } from '../client'
import {
  modelKeys,
  useLastCycle,
  useModelDetail,
  useModelsList,
  useTriggerRefresh,
  type LastCycleResponse,
  type ModelDetailResponse,
  type ModelListResponse,
} from '../models'

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

describe('modelKeys', () => {
  it('all returns the base key', () => {
    expect(modelKeys.all).toEqual(['models'])
  })

  it('list returns the list key', () => {
    expect(modelKeys.list()).toEqual(['models', 'list'])
  })

  it('one returns a key with the model key', () => {
    expect(modelKeys.one('docker:nginx')).toEqual(['models', 'one', 'docker:nginx'])
  })

  it('one encodes colon-bearing keys as-is (no mutation)', () => {
    expect(modelKeys.one('cron:abc123')).toEqual(['models', 'one', 'cron:abc123'])
  })

  it('lastCycle returns the last-cycle key', () => {
    expect(modelKeys.lastCycle()).toEqual(['models', 'cycle', 'last'])
  })
})

describe('useModelsList', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls GET /api/logs/signatures/models and returns data', async () => {
    const mockData: ModelListResponse = {
      models: [
        {
          model_key: 'docker:nginx',
          template_count: 5,
          line_count: 100,
          last_processed_ts: 1000,
          updated_at: 2000,
        },
      ],
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useModelsList(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/models')
    expect(result.current.data).toEqual(mockData)
  })

  it('remains in error state when GET fails', async () => {
    vi.mocked(apiClient.GET).mockResolvedValue({
      error: { code: 'SERVICE_UNAVAILABLE', message: 'drain disabled', details: {} },
      response: fakeResponse(503),
    })

    const { result } = renderHook(() => useModelsList(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/models')
  })
})

describe('useModelDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when enabled=false', () => {
    const { result } = renderHook(() => useModelDetail('docker:nginx', false), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('is disabled when modelKey is empty string even if enabled=true', () => {
    const { result } = renderHook(() => useModelDetail('', true), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('calls GET with path param when enabled=true and key is non-empty', async () => {
    const mockData: ModelDetailResponse = {
      model_key: 'docker:nginx',
      summary: {
        model_key: 'docker:nginx',
        template_count: 2,
        line_count: 50,
        last_processed_ts: 1000,
        updated_at: 2000,
      },
      templates: [],
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useModelDetail('docker:nginx', true), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/models/{model_key}', {
      params: { path: { model_key: 'docker:nginx' } },
    })
    expect(result.current.data).toEqual(mockData)
  })

  it('passes colon-bearing model_key in path param', async () => {
    const mockData: ModelDetailResponse = {
      model_key: 'cron:abc123',
      summary: {
        model_key: 'cron:abc123',
        template_count: 0,
        line_count: 0,
        last_processed_ts: null,
        updated_at: 1234,
      },
      templates: [],
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useModelDetail('cron:abc123', true), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/models/{model_key}', {
      params: { path: { model_key: 'cron:abc123' } },
    })
  })
})

describe('useLastCycle', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls GET /api/logs/signatures/cycle/last and returns data', async () => {
    const mockData: LastCycleResponse = {
      has_run: false,
      lines_processed: 0,
      new_templates: 0,
      models_touched: 0,
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useLastCycle(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures/cycle/last')
    expect(result.current.data).toEqual(mockData)
  })

  it('returns has_run=true data when a cycle has run', async () => {
    const mockData: LastCycleResponse = {
      has_run: true,
      started_at: 1000,
      finished_at: 2000,
      lines_processed: 42,
      new_templates: 3,
      models_touched: 1,
      cycle_status: 'ok',
      error: null,
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useLastCycle(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockData)
  })
})

describe('useTriggerRefresh', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST /api/logs/signatures/refresh when mutate is invoked', async () => {
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: { cycle_id: 'cycle-abc' },
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useTriggerRefresh(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate()

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith('/api/logs/signatures/refresh')
  })
})
