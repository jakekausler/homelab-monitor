import { describe, expect, it } from 'vitest'

import { formatAge } from './relativeTime'

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
