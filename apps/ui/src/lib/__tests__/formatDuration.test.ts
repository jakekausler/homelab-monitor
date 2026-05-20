import { describe, expect, it } from 'vitest'

import { formatDuration } from '@/lib/relativeTime'

describe('formatDuration', () => {
  it('returns em dash for null', () => {
    expect(formatDuration(null)).toBe('—')
  })

  it('returns 0.000s for 0', () => {
    expect(formatDuration(0)).toBe('0.000s')
  })

  it('returns 3-decimal sub-second format for 0.5', () => {
    expect(formatDuration(0.5)).toBe('0.500s')
  })

  it('returns 3-decimal sub-second format for 0.25', () => {
    expect(formatDuration(0.25)).toBe('0.250s')
  })

  it('returns 3-decimal format for 1.234 (under 10s threshold)', () => {
    expect(formatDuration(1.234)).toBe('1.234s')
  })

  it('returns 3-decimal format for 9.999 (just below 10s boundary)', () => {
    expect(formatDuration(9.999)).toBe('9.999s')
  })

  it('returns 1-decimal format for 10.0 (at 10s boundary)', () => {
    expect(formatDuration(10.0)).toBe('10.0s')
  })

  it('returns 1-decimal format for 10.5', () => {
    expect(formatDuration(10.5)).toBe('10.5s')
  })

  it('returns 1-decimal seconds format for 59.9 (boundary below 60)', () => {
    expect(formatDuration(59.9)).toBe('59.9s')
  })

  it('returns minute format for exactly 60', () => {
    expect(formatDuration(60)).toBe('1m 0s')
  })

  it('returns minute format for 323', () => {
    expect(formatDuration(323)).toBe('5m 23s')
  })

  it('returns minute format for 3599 (boundary below 3600)', () => {
    expect(formatDuration(3599)).toBe('59m 59s')
  })

  it('returns hour format for exactly 3600', () => {
    expect(formatDuration(3600)).toBe('1h 0m')
  })

  it('returns hour format for 8054', () => {
    expect(formatDuration(8054)).toBe('2h 14m')
  })

  it('clamps negative values to 0.000s', () => {
    expect(formatDuration(-5)).toBe('0.000s')
  })
})
