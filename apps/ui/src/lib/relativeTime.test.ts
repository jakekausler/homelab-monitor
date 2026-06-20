import { describe, expect, it } from 'vitest'

import { formatAge, formatUptime } from './relativeTime'

describe('formatAge', () => {
  it('returns "just now" for sub-minute values', () => {
    expect(formatAge(0)).toBe('just now')
    expect(formatAge(5)).toBe('just now')
    expect(formatAge(59)).toBe('just now')
  })

  it('returns minutes for <1h', () => {
    expect(formatAge(60)).toBe('1m ago')
    expect(formatAge(300)).toBe('5m ago')
    expect(formatAge(3599)).toBe('59m ago')
  })

  it('returns hours for <1d', () => {
    expect(formatAge(3600)).toBe('1h ago')
    expect(formatAge(7200)).toBe('2h ago')
    expect(formatAge(86399)).toBe('23h ago')
  })

  it('returns days at/above 1d', () => {
    expect(formatAge(86400)).toBe('1d ago')
    expect(formatAge(259200)).toBe('3d ago')
  })

  it('clamps negatives to "just now"', () => {
    expect(formatAge(-10)).toBe('just now')
  })
})

describe('formatUptime', () => {
  it('returns "—" for null/undefined', () => {
    expect(formatUptime(null)).toBe('—')
    expect(formatUptime(undefined)).toBe('—')
  })

  it('returns seconds for < 60s', () => {
    expect(formatUptime(0)).toBe('0s')
    expect(formatUptime(59)).toBe('59s')
  })

  it('returns Xm Ys for 60s–3599s', () => {
    expect(formatUptime(60)).toBe('1m 0s')
    expect(formatUptime(3599)).toBe('59m 59s')
  })

  it('returns Xh Ym for 3600s–86399s', () => {
    expect(formatUptime(3600)).toBe('1h 0m')
    expect(formatUptime(86399)).toBe('23h 59m')
  })

  it('returns Xd Yh for 1d–6d', () => {
    expect(formatUptime(86400)).toBe('1d 0h')
    expect(formatUptime(6 * 86400 + 3600)).toBe('6d 1h')
  })

  it('returns Xw Yd at 7d', () => {
    expect(formatUptime(7 * 86400)).toBe('1w 0d')
    expect(formatUptime(364 * 86400)).toBe('52w 0d')
  })

  it('returns Xy Yw at 365d', () => {
    expect(formatUptime(365 * 86400)).toBe('1y 0w')
  })
})
