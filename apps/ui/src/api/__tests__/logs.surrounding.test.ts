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
import { useSurroundingLogs } from '../logs'

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

describe('useSurroundingLogs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls apiClient.GET with all-services scope when service omitted', async () => {
    const mockData = {
      lines: [],
      anchor_index: null,
      truncated_before: false,
      truncated_after: false,
      degraded: false,
      window_start: new Date('2026-05-07T12:34:00Z'),
      window_end: new Date('2026-05-07T12:35:00Z'),
      queried_at: new Date('2026-05-07T12:34:59Z'),
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () =>
        useSurroundingLogs({
          anchorTs: '2026-05-07T12:34:56.789Z',
          anchorStream: 'stdout',
          anchorMessage: 'test',
          expr: '*',
        }),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/window', {
      params: {
        query: {
          anchor_ts: '2026-05-07T12:34:56.789Z',
          anchor_stream: 'stdout',
          anchor_message: 'test',
          expr: '*',
          before: 100,
          after: 100,
        },
      },
    })
  })

  it('includes service + source_type when service provided', async () => {
    const mockData = {
      lines: [],
      anchor_index: null,
      truncated_before: false,
      truncated_after: false,
      degraded: false,
      window_start: new Date('2026-05-07T12:34:00Z'),
      window_end: new Date('2026-05-07T12:35:00Z'),
      queried_at: new Date('2026-05-07T12:34:59Z'),
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(
      () =>
        useSurroundingLogs({
          anchorTs: '2026-05-07T12:34:56.789Z',
          anchorStream: 'stdout',
          anchorMessage: 'test',
          expr: '*',
          service: 'nginx',
          sourceType: 'docker',
        }),
      { wrapper: makeWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/window', {
      params: {
        query: {
          anchor_ts: '2026-05-07T12:34:56.789Z',
          anchor_stream: 'stdout',
          anchor_message: 'test',
          expr: '*',
          before: 100,
          after: 100,
          service: 'nginx',
          source_type: 'docker',
        },
      },
    })
  })

  it('enabled=false when anchorTs is empty → GET not called', () => {
    const { result } = renderHook(
      () =>
        useSurroundingLogs({
          anchorTs: '',
          anchorStream: 'stdout',
          anchorMessage: 'test',
          expr: '*',
          enabled: false,
        }),
      { wrapper: makeWrapper() },
    )

    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })
})
