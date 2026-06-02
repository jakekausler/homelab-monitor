import { describe, expect, it } from 'vitest'

import { formatCompactCount } from '@/lib/formatCount'

describe('formatCompactCount', () => {
  // --- sub-thousand range ---
  it('formats 0 as "0"', () => expect(formatCompactCount(0)).toBe('0'))
  it('formats 42 as "42"', () => expect(formatCompactCount(42)).toBe('42'))
  it('formats 999 as "999"', () => expect(formatCompactCount(999)).toBe('999'))

  // --- k tier (first decade: one decimal) ---
  it('formats 1000 as "1.0k"', () => expect(formatCompactCount(1_000)).toBe('1.0k'))
  it('formats 1099 as "1.0k" (truncation, not rounding)', () =>
    expect(formatCompactCount(1_099)).toBe('1.0k'))
  it('formats 1100 as "1.1k"', () => expect(formatCompactCount(1_100)).toBe('1.1k'))
  it('formats 9999 as "9.9k"', () => expect(formatCompactCount(9_999)).toBe('9.9k'))

  // --- k tier (remaining decades: integer) ---
  it('formats 10000 as "10k"', () => expect(formatCompactCount(10_000)).toBe('10k'))
  it('formats 11000 as "11k"', () => expect(formatCompactCount(11_000)).toBe('11k'))
  it('formats 999999 as "999k"', () => expect(formatCompactCount(999_999)).toBe('999k'))

  // --- m tier ---
  it('formats 1000000 as "1.0m"', () => expect(formatCompactCount(1_000_000)).toBe('1.0m'))
  it('formats 1100000 as "1.1m"', () => expect(formatCompactCount(1_100_000)).toBe('1.1m'))
  it('formats 9999999 as "9.9m"', () => expect(formatCompactCount(9_999_999)).toBe('9.9m'))
  it('formats 10000000 as "10m"', () => expect(formatCompactCount(10_000_000)).toBe('10m'))
  it('formats 11000000 as "11m"', () => expect(formatCompactCount(11_000_000)).toBe('11m'))
  it('formats 999999999 as "999m"', () => expect(formatCompactCount(999_999_999)).toBe('999m'))

  // --- b tier ---
  it('formats 1000000000 as "1.0b"', () => expect(formatCompactCount(1_000_000_000)).toBe('1.0b'))
  it('formats 11000000000 as "11b"', () => expect(formatCompactCount(11_000_000_000)).toBe('11b'))

  // --- t tier ---
  it('formats 1e12 as "1.0t"', () => expect(formatCompactCount(1e12)).toBe('1.0t'))
  it('formats 5.5e12 as "5.5t"', () => expect(formatCompactCount(5.5e12)).toBe('5.5t'))
  it('formats a very large number (1e15) using the t tier', () => {
    // 1e15 / 1e12 = 1000t integer form
    expect(formatCompactCount(1e15)).toBe('1000t')
  })

  // --- guard: negatives and non-finite ---
  it('returns String(-5) for negative input', () => expect(formatCompactCount(-5)).toBe(String(-5)))
  it('returns String(NaN) for NaN input', () => expect(formatCompactCount(NaN)).toBe(String(NaN)))
  it('returns String(Infinity) for Infinity input', () =>
    expect(formatCompactCount(Infinity)).toBe(String(Infinity)))
  it('returns String(-Infinity) for -Infinity input', () =>
    expect(formatCompactCount(-Infinity)).toBe(String(-Infinity)))
})
