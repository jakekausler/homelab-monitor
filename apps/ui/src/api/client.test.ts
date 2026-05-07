import { describe, expect, it } from 'vitest'

import { ApiError, unwrap } from './client'

function fakeResponse(init: { status: number; headers?: Record<string, string> }): Response {
  return new Response(
    null,
    init.headers === undefined
      ? { status: init.status }
      : { status: init.status, headers: init.headers },
  )
}

describe('unwrap', () => {
  it('returns data when present', () => {
    const data = { hello: 'world' }
    const result = unwrap({
      data,
      response: fakeResponse({ status: 200 }),
    })
    expect(result).toEqual(data)
  })

  it('throws ApiError carrying envelope fields', () => {
    const call = () =>
      unwrap({
        error: {
          error: {
            code: 'wrong_password',
            message: 'invalid username or password',
            details: null,
          },
        },
        response: fakeResponse({ status: 401 }),
      })
    expect(call).toThrow(ApiError)
    expect(call).toThrowError(
      expect.objectContaining({
        status: 401,
        code: 'wrong_password',
        message: 'invalid username or password',
      }),
    )
  })

  it('parses Retry-After when present', () => {
    const call = () =>
      unwrap({
        error: {
          error: {
            code: 'rate_limited',
            message: 'too many login attempts',
            details: null,
          },
        },
        response: fakeResponse({
          status: 429,
          headers: { 'Retry-After': '12' },
        }),
      })
    expect(call).toThrowError(expect.objectContaining({ retryAfterSeconds: 12 }))
  })

  it('falls back to unknown_error for non-envelope errors', () => {
    const call = () =>
      unwrap({
        error: 'plain string',
        response: fakeResponse({ status: 500 }),
      })
    expect(call).toThrowError(
      expect.objectContaining({
        status: 500,
        code: 'unknown_error',
      }),
    )
  })
})
