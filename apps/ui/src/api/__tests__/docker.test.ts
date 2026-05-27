// Project test conventions:
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom
// - Mocking: vi.mock() factory at top, vi.mocked() for typed access
// - Async: async/await + waitFor from @testing-library/react
// - unwrap: mocked inline to return data or throw

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
import {
  useAcceptDockerSuggestion,
  useCustomizeDockerSuggestion,
  useIgnoreDockerSuggestion,
  useSuggestionDefaultProbes,
} from '../docker'

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
// useSuggestionDefaultProbes
// ---------------------------------------------------------------------------

describe('useSuggestionDefaultProbes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetches default probes for a suggestion', async () => {
    const mockData = {
      probes: [
        {
          kind: 'tcp',
          name: 'tcp-8080',
          target_value: 'tcp://host.docker.internal:8080',
          interval_seconds: 60,
          timeout_seconds: 10,
        },
      ],
      reason: 'available',
    }
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSuggestionDefaultProbes('sug-1'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith(
      '/api/integrations/docker/suggestions/{suggestion_id}/default-probes',
      { params: { path: { suggestion_id: 'sug-1' } } },
    )
    expect(result.current.data).toEqual(mockData)
    expect(result.current.data?.reason).toBe('available')
    expect(result.current.data?.probes).toHaveLength(1)
  })

  it('does not fetch when suggestionId is empty', () => {
    const { result } = renderHook(() => useSuggestionDefaultProbes(''), {
      wrapper: makeWrapper(),
    })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiClient.GET).not.toHaveBeenCalled()
  })

  it('surfaces an error when the API fails', async () => {
    vi.mocked(apiClient.GET).mockResolvedValue({
      error: { error: { code: 'not_found', message: 'suggestion not found', details: null } },
      response: fakeResponse(404),
    })
    vi.mocked(unwrap).mockImplementationOnce(() => {
      throw new Error('not_found')
    })

    const { result } = renderHook(() => useSuggestionDefaultProbes('sug-missing'), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toBeInstanceOf(Error)
  })
})

// ---------------------------------------------------------------------------
// useAcceptDockerSuggestion
// ---------------------------------------------------------------------------

describe('useAcceptDockerSuggestion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST accept endpoint with default apply_default_probes=true', async () => {
    const mockData = {
      suggestion: { id: 'sug-1', status: 'accepted', container_name: 'myapp' },
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useAcceptDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-1' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith(
      '/api/integrations/docker/suggestions/{suggestion_id}/accept',
      {
        params: { path: { suggestion_id: 'sug-1' } },
        body: { apply_default_probes: true },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })

  it('passes apply_default_probes=false when specified', async () => {
    const mockData = {
      suggestion: { id: 'sug-2', status: 'accepted', container_name: 'myapp' },
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useAcceptDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-2', applyDefaultProbes: false })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith(
      '/api/integrations/docker/suggestions/{suggestion_id}/accept',
      {
        params: { path: { suggestion_id: 'sug-2' } },
        body: { apply_default_probes: false },
      },
    )
  })

  it('surfaces an error when the POST fails', async () => {
    vi.mocked(apiClient.POST).mockResolvedValue({
      error: { error: { code: 'forbidden', message: 'forbidden', details: null } },
      response: fakeResponse(403),
    })
    vi.mocked(unwrap).mockImplementationOnce(() => {
      throw new Error('forbidden')
    })

    const { result } = renderHook(() => useAcceptDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-3' })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toBeInstanceOf(Error)
  })
})

// ---------------------------------------------------------------------------
// useIgnoreDockerSuggestion
// ---------------------------------------------------------------------------

describe('useIgnoreDockerSuggestion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST ignore endpoint and resolves on success', async () => {
    const mockData = {
      suggestion: { id: 'sug-1', status: 'ignored', container_name: 'myapp' },
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useIgnoreDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-1' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith(
      '/api/integrations/docker/suggestions/{suggestion_id}/ignore',
      {
        params: { path: { suggestion_id: 'sug-1' } },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })

  it('surfaces an error when the POST fails', async () => {
    vi.mocked(apiClient.POST).mockResolvedValue({
      error: { error: { code: 'not_found', message: 'not found', details: null } },
      response: fakeResponse(404),
    })
    vi.mocked(unwrap).mockImplementationOnce(() => {
      throw new Error('not_found')
    })

    const { result } = renderHook(() => useIgnoreDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-missing' })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

// ---------------------------------------------------------------------------
// useCustomizeDockerSuggestion
// ---------------------------------------------------------------------------

describe('useCustomizeDockerSuggestion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST customize endpoint with probes body', async () => {
    const mockData = {
      suggestion: { id: 'sug-1', status: 'accepted', container_name: 'myapp' },
      probes: [],
    }
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: mockData,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useCustomizeDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    const probes = [
      {
        kind: 'tcp' as const,
        name: 'tcp-8080',
        target_value: 'tcp://host.docker.internal:8080',
        interval_seconds: 60,
        timeout_seconds: 10,
      },
    ]

    result.current.mutate({ suggestionId: 'sug-1', probes })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith(
      '/api/integrations/docker/suggestions/{suggestion_id}/customize',
      {
        params: { path: { suggestion_id: 'sug-1' } },
        body: { probes },
      },
    )
    expect(result.current.data).toEqual(mockData)
  })

  it('surfaces an error when the POST fails', async () => {
    vi.mocked(apiClient.POST).mockResolvedValue({
      error: { error: { code: 'validation_error', message: 'bad probes', details: null } },
      response: fakeResponse(422),
    })
    vi.mocked(unwrap).mockImplementationOnce(() => {
      throw new Error('validation_error')
    })

    const { result } = renderHook(() => useCustomizeDockerSuggestion(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ suggestionId: 'sug-1', probes: [] })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})
