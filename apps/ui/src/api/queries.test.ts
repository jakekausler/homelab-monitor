// Project test conventions discovered:
// - Framework: Vitest (no globals — explicit imports required)
// - Environment: jsdom (from vitest.config.ts)
// - React: @vitejs/plugin-react handles JSX transform
// - Async: async/await + waitFor from @testing-library/react
// - Mocking: vi.mock() with factory, vi.mocked() for typed access
// - NO vi.useFakeTimers() — TanStack Query needs real timers

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  apiClient: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  // Real unwrap logic: return data if present, else throw.
  // Tests control this by shaping the mockResolvedValue.
  unwrap: vi.fn((result: { data?: unknown; error?: unknown; response: Response }) => {
    if (result.data !== undefined) return result.data
    throw new Error('mocked api error')
  }),
}))

import { apiClient, unwrap } from './client'
import {
  queryKeys,
  useAlerts,
  useCollectors,
  useCurrentUser,
  useLogin,
  useLogout,
  useMetricsSnapshot,
  useVersion,
} from './queries'

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
// Tests
// ---------------------------------------------------------------------------

describe('queries', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // -------------------------------------------------------------------------
  // useVersion
  // -------------------------------------------------------------------------

  describe('useVersion', () => {
    it('calls GET /api/version and returns unwrapped data', async () => {
      const mockData = { version: '1.0.0' }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockData,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useVersion(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/version')
      expect(result.current.data).toEqual(mockData)
    })

    it('surfaces an error when unwrap throws', async () => {
      vi.mocked(apiClient.GET).mockResolvedValue({
        error: { error: { code: 'server_error', message: 'oops', details: null } },
        response: fakeResponse(500),
      })
      vi.mocked(unwrap).mockImplementationOnce(() => {
        throw new Error('server_error')
      })

      const { result } = renderHook(() => useVersion(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 })
      expect(result.current.error).toBeInstanceOf(Error)
    })
  })

  // -------------------------------------------------------------------------
  // useCurrentUser
  // -------------------------------------------------------------------------

  describe('useCurrentUser', () => {
    it('calls GET /api/auth/me and returns the user on 200', async () => {
      const mockUser = { username: 'admin', display_name: 'Admin' }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockUser,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useCurrentUser(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/auth/me')
      expect(result.current.data).toEqual(mockUser)
    })

    it('returns null (not an error) when the API responds 401', async () => {
      vi.mocked(apiClient.GET).mockResolvedValue({
        response: fakeResponse(401),
      })

      const { result } = renderHook(() => useCurrentUser(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(result.current.data).toBeNull()
      // unwrap should NOT have been called — the 401 short-circuits before it
      expect(unwrap).not.toHaveBeenCalled()
    })
  })

  // -------------------------------------------------------------------------
  // useCollectors
  // -------------------------------------------------------------------------

  describe('useCollectors', () => {
    it('calls GET /api/collectors and returns collector list', async () => {
      const mockCollectors = [{ name: 'ping', status: 'ok', last_run_at: '2026-01-01T00:00:00Z' }]
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockCollectors,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useCollectors(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/collectors')
      expect(result.current.data).toEqual(mockCollectors)
    })

    it('surfaces an error when the API fails', async () => {
      vi.mocked(apiClient.GET).mockResolvedValue({
        error: { error: { code: 'not_found', message: 'not found', details: null } },
        response: fakeResponse(404),
      })
      vi.mocked(unwrap).mockImplementationOnce(() => {
        throw new Error('not_found')
      })

      const { result } = renderHook(() => useCollectors(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isError).toBe(true))
    })
  })

  // -------------------------------------------------------------------------
  // useMetricsSnapshot
  // -------------------------------------------------------------------------

  describe('useMetricsSnapshot', () => {
    it('calls GET /api/metrics/snapshot and returns snapshot data', async () => {
      const mockSnapshot = { hosts: [], alerts_firing: 0 }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockSnapshot,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useMetricsSnapshot(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/metrics/snapshot')
      expect(result.current.data).toEqual(mockSnapshot)
    })
  })

  // -------------------------------------------------------------------------
  // useAlerts
  // -------------------------------------------------------------------------

  describe('useAlerts', () => {
    it('calls GET /api/alerts with no params by default', async () => {
      const mockAlerts = { alerts: [], total: 0 }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockAlerts,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useAlerts(), { wrapper: makeWrapper() })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/alerts', {
        params: { query: {} },
      })
      expect(result.current.data).toEqual(mockAlerts)
    })

    it('passes status param to the query string', async () => {
      const mockAlerts = { alerts: [], total: 0 }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockAlerts,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useAlerts({ status: 'firing' }), {
        wrapper: makeWrapper(),
      })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.GET).toHaveBeenCalledWith('/api/alerts', {
        params: { query: { status: 'firing' } },
      })
    })
  })

  // -------------------------------------------------------------------------
  // useLogin (mutation)
  // -------------------------------------------------------------------------

  describe('useLogin', () => {
    it('calls POST /api/auth/login with the supplied body on mutate()', async () => {
      const mockResponse = { token: 'abc123' }
      vi.mocked(apiClient.POST).mockResolvedValue({
        data: mockResponse,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useLogin(), { wrapper: makeWrapper() })

      result.current.mutate({ username: 'admin', password: 'secret' })

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.POST).toHaveBeenCalledWith('/api/auth/login', {
        body: { username: 'admin', password: 'secret' },
      })
      expect(result.current.data).toEqual(mockResponse)
    })

    it('surfaces an error when login POST fails', async () => {
      vi.mocked(apiClient.POST).mockResolvedValue({
        error: { error: { code: 'wrong_password', message: 'bad creds', details: null } },
        response: fakeResponse(401),
      })
      vi.mocked(unwrap).mockImplementationOnce(() => {
        throw new Error('wrong_password')
      })

      const { result } = renderHook(() => useLogin(), { wrapper: makeWrapper() })

      result.current.mutate({ username: 'admin', password: 'bad' })

      await waitFor(() => expect(result.current.isError).toBe(true))
      expect(result.current.error).toBeInstanceOf(Error)
    })
  })

  // -------------------------------------------------------------------------
  // useLogout (mutation)
  // -------------------------------------------------------------------------

  describe('useLogout', () => {
    it('calls POST /api/auth/logout on mutate() and resolves on 204', async () => {
      vi.mocked(apiClient.POST).mockResolvedValue({
        response: fakeResponse(204),
      })

      const { result } = renderHook(() => useLogout(), { wrapper: makeWrapper() })

      result.current.mutate()

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(apiClient.POST).toHaveBeenCalledWith('/api/auth/logout')
      // unwrap should NOT be called for 204 — the hook returns early
      expect(unwrap).not.toHaveBeenCalled()
    })

    it('calls unwrap when response is not 204', async () => {
      const mockData = {}
      vi.mocked(apiClient.POST).mockResolvedValue({
        data: mockData,
        response: fakeResponse(200),
      })

      const { result } = renderHook(() => useLogout(), { wrapper: makeWrapper() })

      result.current.mutate()

      await waitFor(() => expect(result.current.isSuccess).toBe(true))

      expect(unwrap).toHaveBeenCalled()
    })
  })

  // -------------------------------------------------------------------------
  // queryKeys shape (stable, no API calls)
  // -------------------------------------------------------------------------

  describe('queryKeys', () => {
    it('exposes stable keys for all query hooks', () => {
      expect(queryKeys.currentUser).toEqual(['auth', 'me'])
      expect(queryKeys.version).toEqual(['version'])
      expect(queryKeys.collectors).toEqual(['collectors'])
      expect(queryKeys.metricsSnapshot).toEqual(['metrics', 'snapshot'])
    })

    it('alerts key includes the params object', () => {
      expect(queryKeys.alerts({})).toEqual(['alerts', {}])
      expect(queryKeys.alerts({ status: 'firing' })).toEqual(['alerts', { status: 'firing' }])
    })
  })
})
