import { describe, expect, it } from 'vitest'

import { adlistStatusToBadgeVariant } from './piholeStatus'

describe('adlistStatusToBadgeVariant', () => {
  it('maps "ok" to "ok"', () => {
    expect(adlistStatusToBadgeVariant('ok')).toBe('ok')
    expect(adlistStatusToBadgeVariant('OK')).toBe('ok')
    expect(adlistStatusToBadgeVariant('  Ok ')).toBe('ok')
  })

  it('maps status containing "fail" to "critical"', () => {
    expect(adlistStatusToBadgeVariant('fail')).toBe('critical')
    expect(adlistStatusToBadgeVariant('download failed')).toBe('critical')
    expect(adlistStatusToBadgeVariant('FAILED')).toBe('critical')
  })

  it('maps status containing "error" to "critical"', () => {
    expect(adlistStatusToBadgeVariant('error')).toBe('critical')
    expect(adlistStatusToBadgeVariant('ERROR')).toBe('critical')
    expect(adlistStatusToBadgeVariant('parse error')).toBe('critical')
  })

  it('maps empty string to "muted"', () => {
    expect(adlistStatusToBadgeVariant('')).toBe('muted')
  })

  it('maps "unknown" to "muted"', () => {
    expect(adlistStatusToBadgeVariant('unknown')).toBe('muted')
    expect(adlistStatusToBadgeVariant('UNKNOWN')).toBe('muted')
  })

  it('maps other non-empty strings to "warn"', () => {
    expect(adlistStatusToBadgeVariant('stale')).toBe('warn')
    expect(adlistStatusToBadgeVariant('pending')).toBe('warn')
    expect(adlistStatusToBadgeVariant('other')).toBe('warn')
  })

  it('prioritizes fail/error over other rules', () => {
    expect(adlistStatusToBadgeVariant('fail')).toBe('critical')
    expect(adlistStatusToBadgeVariant('error')).toBe('critical')
  })
})
