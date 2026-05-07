import { QueryClient } from '@tanstack/react-query'
import { isRedirect } from '@tanstack/react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({
  apiClient: {
    GET: vi.fn(),
  },
}))

import { apiClient } from '@/api/client'
import { ensureAuthenticated } from './router'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fakeResponse(status: number): Response {
  return new Response(null, { status })
}

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('router', () => {
  describe('ensureAuthenticated guard', () => {
    beforeEach(() => {
      vi.clearAllMocks()
    })

    it('allows authenticated users: when fetchQuery resolves to a User, guard returns successfully', async () => {
      const mockUser = { username: 'admin', display_name: 'Admin' }
      vi.mocked(apiClient.GET).mockResolvedValue({
        data: mockUser,
        response: fakeResponse(200),
      })

      const queryClient = createTestQueryClient()
      const result = await ensureAuthenticated(queryClient)

      expect(result).toEqual(mockUser)
      expect(apiClient.GET).toHaveBeenCalledWith('/api/auth/me')
    })

    it('redirects unauthenticated users: when fetchQuery resolves to null, guard throws redirect to /login', async () => {
      vi.mocked(apiClient.GET).mockResolvedValue({
        response: fakeResponse(401),
      })

      const queryClient = createTestQueryClient()

      let redirectThrown = false
      let redirectTarget = ''
      try {
        await ensureAuthenticated(queryClient)
      } catch (err) {
        if (isRedirect(err)) {
          redirectThrown = true
          redirectTarget = (err.options.to as string) ?? ''
        }
      }

      expect(redirectThrown).toBe(true)
      expect(redirectTarget).toBe('/login')
    })

    it('surfaces fetch errors: when fetchQuery rejects with non-401 error, guard re-raises', async () => {
      vi.mocked(apiClient.GET).mockRejectedValue(new Error('network-error'))

      const queryClient = createTestQueryClient()

      let errorThrown = false
      let errorMessage = ''
      try {
        await ensureAuthenticated(queryClient)
      } catch (err) {
        if (err instanceof Error) {
          errorThrown = true
          errorMessage = err.message
        }
      }

      expect(errorThrown).toBe(true)
      expect(errorMessage).toBe('network-error')
    })

    it('uses cached result when already in query cache', async () => {
      const mockUser = { username: 'admin', display_name: 'Admin' }
      const queryClient = createTestQueryClient()

      // Pre-populate the cache
      queryClient.setQueryData(['auth', 'me'], mockUser)

      const result = await ensureAuthenticated(queryClient)

      expect(result).toEqual(mockUser)
      // fetchQuery should not have been called — cache was used
      expect(apiClient.GET).not.toHaveBeenCalled()
    })
  })
})
