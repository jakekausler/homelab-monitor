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
import { computeCopyName, savedRowToCreateRequest } from '@/api/savedLogQueries'
import {
  useSavedLogQueriesQuery,
  useCreateSavedLogQuery,
  useRenameSavedLogQuery,
  useDeleteSavedLogQuery,
  useUpdateSavedLogQuery,
  savedLogQueryKeys,
} from '@/api/savedLogQueries'
import type { Schema } from '@/api/types'

type SavedQuery = Schema<'SavedQueryResponse'>

// ---------------------------------------------------------------------------
// computeCopyName
// ---------------------------------------------------------------------------

describe('computeCopyName', () => {
  it('appends "(copy)" when no copy exists yet', () => {
    expect(computeCopyName('nginx errors', [])).toBe('nginx errors (copy)')
  })

  it('produces "(copy 1)" when "(copy)" is already taken', () => {
    expect(computeCopyName('nginx errors', ['nginx errors (copy)'])).toBe('nginx errors (copy 1)')
  })

  it('copies of a "(copy)" base: strips existing copy suffix and retries', () => {
    // Source already ends in "(copy)" but that name is taken → should produce "(copy 1)"
    expect(computeCopyName('nginx errors (copy)', ['nginx errors (copy)'])).toBe(
      'nginx errors (copy 1)',
    )
  })

  it('skips taken copy numbers and produces the first free one', () => {
    // "x (copy)" and "x (copy 1)" taken → next candidate is "x (copy 2)"
    expect(computeCopyName('x (copy 3)', ['x (copy)', 'x (copy 1)'])).toBe('x (copy 2)')
  })

  it('produces "(copy)" for a plain name with no existing copies', () => {
    expect(computeCopyName('my query', [])).toBe('my query (copy)')
  })

  it('is case-sensitive — "Nginx Errors (copy)" does not collide with "nginx errors (copy)"', () => {
    // The new candidate "nginx errors (copy)" is NOT taken by the differently-cased entry
    expect(computeCopyName('nginx errors', ['Nginx Errors (copy)'])).toBe('nginx errors (copy)')
  })

  it('increments past multiple taken copies in sequence', () => {
    const taken = ['foo (copy)', 'foo (copy 1)', 'foo (copy 2)']
    expect(computeCopyName('foo', taken)).toBe('foo (copy 3)')
  })
})

// ---------------------------------------------------------------------------
// savedRowToCreateRequest
// ---------------------------------------------------------------------------

function makeSavedQuery(overrides: Partial<SavedQuery> = {}): SavedQuery {
  return {
    id: 1,
    name: 'original name',
    logs_ql: '_msg:"error"',
    selected_services: [{ service: 'nginx', source_type: 'docker' }],
    advanced_mode: false,
    since_preset: '15m',
    range_start_iso: null,
    range_end_iso: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('savedRowToCreateRequest', () => {
  it('uses the provided newName', () => {
    const row = makeSavedQuery({ name: 'original' })
    const req = savedRowToCreateRequest(row, 'new name')
    expect(req.name).toBe('new name')
  })

  it('preset-range row: sets since_preset and omits range_start_iso/range_end_iso', () => {
    const row = makeSavedQuery({ since_preset: '1h', range_start_iso: null, range_end_iso: null })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.since_preset).toBe('1h')
    expect(req).not.toHaveProperty('range_start_iso')
    expect(req).not.toHaveProperty('range_end_iso')
  })

  it('custom-range row: sets range_start_iso + range_end_iso and omits since_preset', () => {
    const row = makeSavedQuery({
      since_preset: null,
      range_start_iso: '2026-01-01T00:00:00Z',
      range_end_iso: '2026-01-01T06:00:00Z',
    })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.range_start_iso).toBe('2026-01-01T00:00:00Z')
    expect(req.range_end_iso).toBe('2026-01-01T06:00:00Z')
    expect(req).not.toHaveProperty('since_preset')
  })

  it('preserves logs_ql', () => {
    const row = makeSavedQuery({ logs_ql: 'service:home-assistant AND severity:error' })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.logs_ql).toBe('service:home-assistant AND severity:error')
  })

  it('preserves selected_services (mapped to plain identity objects)', () => {
    const row = makeSavedQuery({
      selected_services: [
        { service: 'nginx', source_type: 'docker' },
        { service: 'sshd', source_type: 'systemd' },
      ],
    })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.selected_services).toEqual([
      { service: 'nginx', source_type: 'docker' },
      { service: 'sshd', source_type: 'systemd' },
    ])
  })

  it('preserves advanced_mode when true', () => {
    const row = makeSavedQuery({ advanced_mode: true })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.advanced_mode).toBe(true)
  })

  it('preserves advanced_mode when false', () => {
    const row = makeSavedQuery({ advanced_mode: false })
    const req = savedRowToCreateRequest(row, 'copy')
    expect(req.advanced_mode).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Hook test helpers
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

const MOCK_SAVED_QUERY: Schema<'SavedQueryResponse'> = {
  id: 1,
  name: 'nginx errors',
  logs_ql: '_msg:"error"',
  selected_services: [{ service: 'nginx', source_type: 'docker' }],
  advanced_mode: false,
  since_preset: '15m',
  range_start_iso: null,
  range_end_iso: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const MOCK_LIST_RESPONSE: Schema<'SavedQueriesListResponse'> = {
  saved_queries: [MOCK_SAVED_QUERY],
}

// ---------------------------------------------------------------------------
// savedLogQueryKeys
// ---------------------------------------------------------------------------

describe('savedLogQueryKeys', () => {
  it('all returns expected array', () => {
    expect(savedLogQueryKeys.all).toEqual(['saved-log-queries'])
  })

  it('list() returns expected array', () => {
    expect(savedLogQueryKeys.list()).toEqual(['saved-log-queries', 'list'])
  })
})

// ---------------------------------------------------------------------------
// useSavedLogQueriesQuery
// ---------------------------------------------------------------------------

describe('useSavedLogQueriesQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('fetches the saved queries list and returns unwrapped data', async () => {
    vi.mocked(apiClient.GET).mockResolvedValue({
      data: MOCK_LIST_RESPONSE,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useSavedLogQueriesQuery(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.GET).toHaveBeenCalledWith('/api/logs/saved-queries', {})
    expect(result.current.data).toEqual(MOCK_LIST_RESPONSE)
  })

  it('surfaces error when API returns error response', async () => {
    vi.mocked(apiClient.GET).mockRejectedValue(new Error('server_error'))

    const { result } = renderHook(() => useSavedLogQueriesQuery(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error).toBeInstanceOf(Error)
  })
})

// ---------------------------------------------------------------------------
// useCreateSavedLogQuery
// ---------------------------------------------------------------------------

describe('useCreateSavedLogQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls POST /api/logs/saved-queries with the body and returns the saved query', async () => {
    vi.mocked(apiClient.POST).mockResolvedValue({
      data: MOCK_SAVED_QUERY,
      response: fakeResponse(201),
    })

    const { result } = renderHook(() => useCreateSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    const body: Schema<'SaveQueryCreateRequest'> = {
      name: 'nginx errors',
      logs_ql: '_msg:"error"',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
      advanced_mode: false,
      since_preset: '15m',
    }

    result.current.mutate(body)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.POST).toHaveBeenCalledWith('/api/logs/saved-queries', { body })
    expect(result.current.data).toEqual(MOCK_SAVED_QUERY)
  })

  it('surfaces error when POST fails', async () => {
    vi.mocked(apiClient.POST).mockRejectedValue(new Error('conflict'))

    const { result } = renderHook(() => useCreateSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      name: 'nginx errors',
      logs_ql: '_msg:"error"',
      selected_services: [],
      advanced_mode: false,
      since_preset: '15m',
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

// ---------------------------------------------------------------------------
// useRenameSavedLogQuery
// ---------------------------------------------------------------------------

describe('useRenameSavedLogQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls PATCH /api/logs/saved-queries/{query_id} with name body', async () => {
    vi.mocked(apiClient.PATCH).mockResolvedValue({
      data: { ...MOCK_SAVED_QUERY, name: 'renamed' },
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useRenameSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ id: 1, name: 'renamed' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.PATCH).toHaveBeenCalledWith('/api/logs/saved-queries/{query_id}', {
      params: { path: { query_id: 1 } },
      body: { name: 'renamed' },
    })
    expect(result.current.data).toMatchObject({ name: 'renamed' })
  })

  it('surfaces error when PATCH fails', async () => {
    vi.mocked(apiClient.PATCH).mockRejectedValue(new Error('not_found'))

    const { result } = renderHook(() => useRenameSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ id: 99, name: 'whatever' })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

// ---------------------------------------------------------------------------
// useDeleteSavedLogQuery
// ---------------------------------------------------------------------------

describe('useDeleteSavedLogQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls DELETE /api/logs/saved-queries/{query_id} and resolves void on 204', async () => {
    vi.mocked(apiClient.DELETE).mockResolvedValue({
      response: fakeResponse(204),
    } as unknown as Awaited<ReturnType<typeof apiClient.DELETE>>)

    const { result } = renderHook(() => useDeleteSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ id: 1 })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.DELETE).toHaveBeenCalledWith('/api/logs/saved-queries/{query_id}', {
      params: { path: { query_id: 1 } },
    })
    expect(result.current.data).toBeUndefined()
  })

  it('surfaces error when DELETE fails', async () => {
    vi.mocked(apiClient.DELETE).mockRejectedValue(new Error('not_found'))

    const { result } = renderHook(() => useDeleteSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({ id: 99 })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})

// ---------------------------------------------------------------------------
// useUpdateSavedLogQuery
// ---------------------------------------------------------------------------

describe('useUpdateSavedLogQuery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('calls PUT /api/logs/saved-queries/{query_id} with the full body', async () => {
    const updatedQuery = { ...MOCK_SAVED_QUERY, logs_ql: 'updated-expr' }
    vi.mocked(apiClient.PUT).mockResolvedValue({
      data: updatedQuery,
      response: fakeResponse(200),
    })

    const { result } = renderHook(() => useUpdateSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    const body: Schema<'SaveQueryCreateRequest'> = {
      name: 'nginx errors',
      logs_ql: 'updated-expr',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
      advanced_mode: false,
      since_preset: '15m',
    }

    result.current.mutate({ id: 1, body })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(apiClient.PUT).toHaveBeenCalledWith('/api/logs/saved-queries/{query_id}', {
      params: { path: { query_id: 1 } },
      body,
    })
    expect(result.current.data).toEqual(updatedQuery)
  })

  it('surfaces error when PUT fails', async () => {
    vi.mocked(apiClient.PUT).mockRejectedValue(new Error('not_found'))

    const { result } = renderHook(() => useUpdateSavedLogQuery(), {
      wrapper: makeWrapper(),
    })

    result.current.mutate({
      id: 99,
      body: {
        name: 'x',
        logs_ql: 'x',
        selected_services: [],
        advanced_mode: false,
        since_preset: '1h',
      },
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
  })
})
