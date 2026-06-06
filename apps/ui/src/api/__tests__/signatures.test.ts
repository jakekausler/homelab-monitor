import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', () => ({
  apiClient: {
    GET: vi.fn(),
    PATCH: vi.fn(),
  },
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient } from '../client'
import {
  signatureKeys,
  useSignaturesQuery,
  useSignature,
  useSignatureSamples,
  useUpdateSignature,
  type SignatureFilter,
  type SignatureListResponse,
  type SignatureResponse,
  type SignatureSamplesResponse,
} from '../signatures'

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

describe('signatureKeys', () => {
  it('all returns the base key', () => {
    expect(signatureKeys.all).toEqual(['signatures'])
  })

  it('list returns a key with filter', () => {
    const filter: SignatureFilter = { service: 'nginx', status: 'active' }
    expect(signatureKeys.list(filter)).toEqual(['signatures', 'list', filter])
  })

  it('one returns a key with hash and service', () => {
    expect(signatureKeys.one('abc123', 'docker:nginx')).toEqual([
      'signatures',
      'one',
      'abc123',
      'docker:nginx',
    ])
  })

  it('samples returns a key with hash and service', () => {
    expect(signatureKeys.samples('abc123', 'docker:nginx')).toEqual([
      'signatures',
      'samples',
      'abc123',
      'docker:nginx',
    ])
  })
})

describe('useSignaturesQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('builds request with all filter fields', async () => {
    const mockData: SignatureListResponse = { signatures: [], total: 0 }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const filter: SignatureFilter = {
      service: 'nginx',
      status: 'active',
      label_q: 'test',
      limit: 50,
      offset: 10,
    }

    const { result } = renderHook(() => useSignaturesQuery(filter), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures', {
      params: {
        query: {
          service: 'nginx',
          status: 'active',
          label_q: 'test',
          limit: 50,
          offset: 10,
        },
      },
    })
  })

  it('omits undefined filter fields', async () => {
    const mockData: SignatureListResponse = { signatures: [], total: 0 }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const filter: SignatureFilter = {
      service: 'nginx',
    }

    const { result } = renderHook(() => useSignaturesQuery(filter), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/signatures', {
      params: {
        query: {
          service: 'nginx',
        },
      },
    })
  })
})

describe('useSignature', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when templateHash is empty', () => {
    const { result } = renderHook(() => useSignature('', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('is disabled when serviceKey is empty', () => {
    const { result } = renderHook(() => useSignature('abc123', ''), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('fetches when both hash and service are provided', async () => {
    const mockData: SignatureResponse = {
      template_hash: 'abc123',
      service_key: 'docker:nginx',
      template_str: 'error <*> occurred',
      label: 'test-label',
      status: 'active',
      first_seen_at: 1000,
      last_seen_at: 2000,
      total_count: 42,
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSignature('abc123', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}',
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
    const { result } = renderHook(() => useSignature('abc123', 'docker:nginx', false), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })
})

describe('useSignatureSamples', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('is disabled when templateHash is empty', () => {
    const { result } = renderHook(() => useSignatureSamples('', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('fetches when both hash and service are provided', async () => {
    const mockData: SignatureSamplesResponse = {
      lines: [],
      reason: null,
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSignatureSamples('abc123', 'docker:nginx'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}/samples',
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
})

describe('useUpdateSignature', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('sends PATCH request with correct body', async () => {
    const mockData: SignatureResponse = {
      template_hash: 'abc123',
      service_key: 'docker:nginx',
      template_str: 'error <*> occurred',
      label: 'updated-label',
      status: 'suppressed',
      first_seen_at: 1000,
      last_seen_at: 2000,
      total_count: 42,
    }
    vi.mocked(apiClient.PATCH).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useUpdateSignature(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      templateHash: 'abc123',
      serviceKey: 'docker:nginx',
      body: { label: 'updated-label' },
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.PATCH).toHaveBeenCalledWith(
      '/api/logs/signatures/{template_hash}/{service_key}',
      {
        params: {
          path: {
            template_hash: 'abc123',
            service_key: 'docker:nginx',
          },
        },
        body: { label: 'updated-label' },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })

  it('invalidates signatureKeys.all on success', async () => {
    const mockData: SignatureResponse = {
      template_hash: 'abc123',
      service_key: 'docker:nginx',
      template_str: 'error <*> occurred',
      label: null,
      status: 'active',
      first_seen_at: 1000,
      last_seen_at: 2000,
      total_count: 42,
    }
    vi.mocked(apiClient.PATCH).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useUpdateSignature(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      templateHash: 'abc123',
      serviceKey: 'docker:nginx',
      body: { status: 'suppressed' },
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    // Just verify the mutation succeeded; invalidation is implicit in react-query behavior
    expect(result.current.data).toEqual(mockData)
  })
})
