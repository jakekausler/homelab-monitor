import { afterEach, describe, expect, it } from 'vitest'

import { getCsrfToken } from './csrf'

const ORIGINAL_COOKIE = document.cookie

function resetCookies() {
  // Clear any cookie set by a previous test by expiring it.
  for (const c of document.cookie.split(';')) {
    const eq = c.indexOf('=')
    const name = eq > -1 ? c.slice(0, eq).trim() : c.trim()
    if (name.length > 0) {
      document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`
    }
  }
}

describe('getCsrfToken', () => {
  afterEach(() => {
    resetCookies()
    document.cookie = ORIGINAL_COOKIE
  })

  it('returns null when no cookie is set', () => {
    resetCookies()
    expect(getCsrfToken()).toBeNull()
  })

  it('returns the token value when present', () => {
    document.cookie = 'homelab_monitor_csrf=abc123; path=/'
    expect(getCsrfToken()).toBe('abc123')
  })

  it('decodes URL-encoded values', () => {
    document.cookie = 'homelab_monitor_csrf=a%2Fb%3Dc; path=/'
    expect(getCsrfToken()).toBe('a/b=c')
  })

  it('returns null when the cookie value is empty', () => {
    document.cookie = 'homelab_monitor_csrf=; path=/'
    expect(getCsrfToken()).toBeNull()
  })

  it('ignores unrelated cookies', () => {
    document.cookie = 'something_else=ignored; path=/'
    expect(getCsrfToken()).toBeNull()
  })
})
