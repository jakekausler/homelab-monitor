import { describe, expect, it } from 'vitest'

import { formatAbsolute, formatRelative } from '@/lib/relativeTime'

const NOW = Date.parse('2026-05-11T12:00:00Z')

describe('formatRelative', () => {
  it('returns em-dash for null/undefined', () => {
    expect(formatRelative(null, NOW)).toBe('—')
    expect(formatRelative(undefined, NOW)).toBe('—')
    expect(formatRelative('', NOW)).toBe('—')
  })

  it('formats minutes ago', () => {
    expect(formatRelative('2026-05-11T11:55:00Z', NOW)).toBe('5 minutes ago')
  })

  it('formats hours from now', () => {
    expect(formatRelative('2026-05-11T15:00:00Z', NOW)).toBe('in 3 hours')
  })

  it('falls back to absolute over 30 days', () => {
    const out = formatRelative('2025-01-01T00:00:00Z', NOW)
    expect(out).not.toBe('—')
    expect(out).not.toMatch(/ago|in /)
  })

  it('returns raw input on unparseable', () => {
    expect(formatRelative('not-a-date', NOW)).toBe('not-a-date')
  })
})

describe('formatAbsolute', () => {
  it('returns em-dash for empty', () => {
    expect(formatAbsolute(null)).toBe('—')
  })
  it('formats valid ISO', () => {
    const out = formatAbsolute('2026-05-11T12:00:00Z')
    expect(out).not.toBe('—')
    expect(out.length).toBeGreaterThan(0)
  })
})
