import createClient, { type Middleware } from 'openapi-fetch'

import type { paths } from './schema'
import { CSRF_HEADER, getCsrfToken } from './csrf'

const STATE_CHANGING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * CSRF middleware: on every state-changing request, read the
 * `homelab_monitor_csrf` cookie and inject it as `X-CSRF-Token`.
 */
const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (STATE_CHANGING_METHODS.has(request.method.toUpperCase())) {
      const token = getCsrfToken()
      if (token !== null) {
        request.headers.set(CSRF_HEADER, token)
      }
    }
    return request
  },
}

export const apiClient = createClient<paths>({
  baseUrl: '/',
  credentials: 'include',
  headers: {
    'Content-Type': 'application/json',
  },
})

apiClient.use(csrfMiddleware)

export interface ApiErrorPayload {
  code: string
  message: string
  details: Record<string, unknown> | null
}

/**
 * Thrown when an API call returns a non-2xx with the standard error envelope
 * `{ "error": { code, message, details } }`. Carries the HTTP status code
 * and any `Retry-After` header value so callers can surface countdowns.
 */
export class ApiError extends Error {
  public readonly status: number
  public readonly code: string
  public readonly retryAfterSeconds: number | null
  public readonly details: Record<string, unknown> | null

  constructor(args: {
    status: number
    code: string
    message: string
    retryAfterSeconds: number | null
    details: Record<string, unknown> | null
  }) {
    super(args.message)
    this.name = 'ApiError'
    this.status = args.status
    this.code = args.code
    this.retryAfterSeconds = args.retryAfterSeconds
    this.details = args.details
  }
}

interface ErrorEnvelope {
  error: ApiErrorPayload
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== 'object' || value === null) return false
  const env = value as { error?: unknown }
  if (typeof env.error !== 'object' || env.error === null) return false
  const err = env.error as { code?: unknown; message?: unknown }
  return typeof err.code === 'string' && typeof err.message === 'string'
}

function parseRetryAfter(headers: Headers): number | null {
  const raw = headers.get('Retry-After')
  if (raw === null) return null
  const seconds = Number.parseInt(raw, 10)
  return Number.isFinite(seconds) ? seconds : null
}

/**
 * Convert an openapi-fetch result into a thrown ApiError on failure.
 *
 * openapi-fetch returns `{ data, error, response }` instead of throwing.
 * For TanStack Query we want a thrown exception so `useQuery` can surface
 * it via `error`. Returns the unwrapped data on success.
 */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined) {
    return result.data
  }
  const status = result.response.status
  const retryAfterSeconds = parseRetryAfter(result.response.headers)
  if (isErrorEnvelope(result.error)) {
    throw new ApiError({
      status,
      code: result.error.error.code,
      message: result.error.error.message,
      retryAfterSeconds,
      details: result.error.error.details,
    })
  }
  throw new ApiError({
    status,
    code: 'unknown_error',
    message: `request failed: ${String(status)}`,
    retryAfterSeconds,
    details: null,
  })
}
