import { describe, expect, it } from 'vitest'

import { formatSourceHash } from '../sourceHash'

describe('formatSourceHash', () => {
  it('returns em-dash for null', () => {
    expect(formatSourceHash(null)).toBe('—')
  })

  it('returns em-dash for undefined', () => {
    expect(formatSourceHash(undefined)).toBe('—')
  })

  it('returns em-dash for empty string', () => {
    expect(formatSourceHash('')).toBe('—')
  })

  it('preserves OVERSIZED:context_too_large sentinel unchanged', () => {
    expect(formatSourceHash('OVERSIZED:context_too_large')).toBe('OVERSIZED:context_too_large')
  })

  it('preserves OVERSIZED:permission_denied sentinel unchanged', () => {
    expect(formatSourceHash('OVERSIZED:permission_denied')).toBe('OVERSIZED:permission_denied')
  })

  it('truncates 64-char hex hash to first 12 chars + ellipsis', () => {
    const full = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'
    expect(formatSourceHash(full)).toBe('a1b2c3d4e5f6…')
  })

  it('truncates any string >= 12 chars to first 12 + ellipsis', () => {
    expect(formatSourceHash('abcdefghijklmnop')).toBe('abcdefghijkl…')
  })

  it('returns short string (< 12 chars) unchanged', () => {
    expect(formatSourceHash('abc')).toBe('abc')
    expect(formatSourceHash('short')).toBe('short')
  })

  it('returns exactly-12-char string with ellipsis', () => {
    // 12 chars: hits the >= branch
    expect(formatSourceHash('abcdefghijkl')).toBe('abcdefghijkl…')
  })

  it('handles a realistic source hash from the hasher', () => {
    // sha256 hex is 64 chars — should always truncate
    const sha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    const result = formatSourceHash(sha256)
    expect(result).toBe('e3b0c44298fc…')
    expect(result.endsWith('…')).toBe(true)
    expect(result.length).toBe(13) // 12 + ellipsis char
  })
})
