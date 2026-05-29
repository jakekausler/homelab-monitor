import { describe, expect, it } from 'vitest'

import { formatAbsolute, formatLogTimestamp, formatRelative } from '@/lib/relativeTime'

const NOW = Date.parse('2026-05-11T12:00:00Z')

describe('formatRelative', () => {
  it('returns em-dash for null/undefined', () => {
    expect(formatRelative(null, NOW)).toBe('—')
    expect(formatRelative(undefined, NOW)).toBe('—')
    expect(formatRelative('', NOW)).toBe('—')
  })

  it('returns "just now" for sub-1s delta', () => {
    const almostNow = new Date(NOW + 500).toISOString()
    expect(formatRelative(almostNow, NOW)).toBe('just now')
  })

  it('formats seconds ago', () => {
    expect(formatRelative('2026-05-11T11:59:45Z', NOW)).toBe('15s ago')
  })

  it('formats minutes and seconds ago', () => {
    expect(formatRelative('2026-05-11T11:54:30Z', NOW)).toBe('5m 30s ago')
  })

  it('formats minutes and seconds in future', () => {
    expect(formatRelative('2026-05-11T12:05:30Z', NOW)).toBe('in 5m 30s')
  })

  it('formats hours and minutes ago', () => {
    expect(formatRelative('2026-05-11T06:48:00Z', NOW)).toBe('5h 12m ago')
  })

  it('formats hours and minutes in future', () => {
    expect(formatRelative('2026-05-11T17:12:00Z', NOW)).toBe('in 5h 12m')
  })

  it('formats days and hours ago (within 3 days)', () => {
    expect(formatRelative('2026-05-09T08:00:00Z', NOW)).toBe('2d 4h ago')
  })

  it('formats days and hours in future (within 3 days)', () => {
    expect(formatRelative('2026-05-13T16:00:00Z', NOW)).toBe('in 2d 4h')
  })

  it('formats exactly 3 days as days and hours', () => {
    expect(formatRelative('2026-05-08T12:00:00Z', NOW)).toBe('3d 0h ago')
  })

  it('formats days only when > 3 days ago', () => {
    expect(formatRelative('2026-04-11T12:00:00Z', NOW)).toBe('30d ago')
  })

  it('formats days only when > 3 days in future', () => {
    expect(formatRelative('2026-06-10T12:00:00Z', NOW)).toBe('in 30d')
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

describe('formatLogTimestamp', () => {
  it('formats nanosecond ISO to friendly UTC', () => {
    expect(formatLogTimestamp('2026-05-29T12:53:53.162712958Z')).toBe('2026-05-29 12:53:53 UTC')
  })

  it('formats millisecond ISO to friendly UTC', () => {
    expect(formatLogTimestamp('2026-05-21T14:30:00.123Z')).toBe('2026-05-21 14:30:00 UTC')
  })

  it('formats plain Z ISO to friendly UTC', () => {
    expect(formatLogTimestamp('2026-05-21T14:30:00Z')).toBe('2026-05-21 14:30:00 UTC')
  })

  it('treats space-separated no-zone input as UTC (idempotent-ish)', () => {
    expect(formatLogTimestamp('2026-05-21 14:30:00')).toBe('2026-05-21 14:30:00 UTC')
  })

  it('drops a positive offset suffix WITHOUT shifting the clock', () => {
    expect(formatLogTimestamp('2026-05-21T14:30:00+02:00')).toBe('2026-05-21 14:30:00 UTC')
  })

  it('drops a negative compact offset suffix WITHOUT shifting the clock', () => {
    expect(formatLogTimestamp('2026-05-21T14:30:00-0500')).toBe('2026-05-21 14:30:00 UTC')
  })

  it('passes through a non-ISO string unchanged', () => {
    expect(formatLogTimestamp('not-a-timestamp')).toBe('not-a-timestamp')
  })

  it('returns empty string for empty/null/undefined', () => {
    expect(formatLogTimestamp('')).toBe('')
    expect(formatLogTimestamp(null)).toBe('')
    expect(formatLogTimestamp(undefined)).toBe('')
  })
})
