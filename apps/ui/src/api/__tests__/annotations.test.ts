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
  annotationKeys,
  useSignatureAnnotations,
  useAddAnnotation,
  useDeleteAnnotation,
  type AnnotationListResponse,
  type AnnotationResponse,
} from '../annotations'

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

describe('annotationKeys', () => {
  it('all returns the base key', () => {
    expect(annotationKeys.all).toEqual(['annotations'])
  })

  it('list returns a key with hash and service', () => {
    expect(annotationKeys.list('h', 's')).toEqual(['annotations', 'list', 'h', 's'])
  })
})

describe('useSignatureAnnotations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when templateHash is empty', () => {
    const { result } = renderHook(() => useSignatureAnnotations('', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('is disabled when serviceKey is empty', () => {
    const { result } = renderHook(() => useSignatureAnnotations('abc123', ''), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('fetches with correct path params', async () => {
    const mockData: AnnotationListResponse = { annotations: [] }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSignatureAnnotations('abc123', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}/annotations',
      {
        params: {
          path: {
            template_hash: 'abc123',
            service_key: 'docker:nginx',
          },
        },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })

  it('respects enabled flag', () => {
    const { result } = renderHook(() => useSignatureAnnotations('abc123', 'docker:nginx', false), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })
})

describe('useAddAnnotation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST with correct path and body', async () => {
    const mockData: AnnotationResponse = {
      id: 1,
      template_hash: 'h',
      service_key: 's',
      note: 'hi',
      author: 'me',
      created_at: '2026-01-01T00:00:00+00:00',
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(201),
    })

    const { result } = renderHook(() => useAddAnnotation(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      templateHash: 'h',
      serviceKey: 's',
      body: { note: 'hi' },
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}/annotations',
      {
        params: {
          path: {
            template_hash: 'h',
            service_key: 's',
          },
        },
        body: { note: 'hi' },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })
})

describe('useDeleteAnnotation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls DELETE with correct path params and handles 204', async () => {
    vi.mocked(apiClient.DELETE).mockResolvedValue({
      response: fakeResponse(204),
    } as unknown as Awaited<ReturnType<typeof apiClient.DELETE>>)

    const { result } = renderHook(() => useDeleteAnnotation(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      templateHash: 'h',
      serviceKey: 's',
      annotationId: 7,
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.DELETE).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}/annotations/{annotation_id}',
      {
        params: {
          path: {
            template_hash: 'h',
            service_key: 's',
            annotation_id: 7,
          },
        },
      },
    )
  })
})
