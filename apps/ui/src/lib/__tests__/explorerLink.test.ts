import { describe, expect, it } from 'vitest'

import { buildExplorerUrl, EXPLORER_URL_KEYS } from '../explorerLink'

// Mirrors the keys the /logs route accepts (apps/ui/src/router.tsx
// validateSearch:148-162). Kept in sync manually; the subset guard below fails
// loudly if EXPLORER_URL_KEYS ever drifts outside this set.
// NOTE: This is a hand-copied mirror of the router's validateSearch keys — it
// is NOT automatically derived. If the /logs route search schema changes, update
// this array to match. The live integration guard is the memory-router test in
// OpenInExplorerButton.test.tsx, which exercises the real route + URL round-trip.
const ROUTE_ACCEPTED_KEYS = ['q', 'logsql', 'since', 'start', 'end', 'services'] as const

describe('buildExplorerUrl', () => {
  it('maps logsQl to the logsql param', () => {
    const url = buildExplorerUrl({ logsQl: 'service:"nginx"' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('logsql')).toBe('service:"nginx"')
    expect(params.get('q')).toBeNull()
  })

  it('maps plainText to the q param', () => {
    const url = buildExplorerUrl({ plainText: 'error timeout' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('q')).toBe('error timeout')
    expect(params.get('logsql')).toBeNull()
  })

  it('logsQl wins when both logsQl and plainText are given', () => {
    const url = buildExplorerUrl({ logsQl: 'service:"x"', plainText: 'ignored' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('logsql')).toBe('service:"x"')
    expect(params.get('q')).toBeNull()
  })

  it('treats empty-string logsQl as absent and falls back to plainText', () => {
    const url = buildExplorerUrl({ logsQl: '', plainText: 'fallback' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('q')).toBe('fallback')
    expect(params.get('logsql')).toBeNull()
  })

  it('omits both query params when neither is provided', () => {
    const url = buildExplorerUrl({ sincePreset: '1h' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('q')).toBeNull()
    expect(params.get('logsql')).toBeNull()
  })

  it('maps sincePreset to the since param', () => {
    const url = buildExplorerUrl({ sincePreset: '6h' })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('since')).toBe('6h')
  })

  it('maps rangeStart + rangeEnd to ISO-Z start/end params', () => {
    const start = new Date('2026-05-01T00:00:00.000Z')
    const end = new Date('2026-05-01T01:00:00.000Z')
    const url = buildExplorerUrl({ rangeStart: start, rangeEnd: end })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('start')).toBe('2026-05-01T00:00:00.000Z')
    expect(params.get('end')).toBe('2026-05-01T01:00:00.000Z')
  })

  it('allows rangeStart without rangeEnd (open-ended)', () => {
    const start = new Date('2026-05-01T00:00:00.000Z')
    const url = buildExplorerUrl({ rangeStart: start })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('start')).toBe('2026-05-01T00:00:00.000Z')
    expect(params.get('end')).toBeNull()
  })

  it('allows rangeEnd without rangeStart', () => {
    const end = new Date('2026-05-01T01:00:00.000Z')
    const url = buildExplorerUrl({ rangeEnd: end })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('end')).toBe('2026-05-01T01:00:00.000Z')
    expect(params.get('start')).toBeNull()
  })

  it('sincePreset takes precedence over rangeStart/rangeEnd', () => {
    const start = new Date('2026-05-01T00:00:00.000Z')
    const end = new Date('2026-05-01T01:00:00.000Z')
    const url = buildExplorerUrl({ sincePreset: '24h', rangeStart: start, rangeEnd: end })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('since')).toBe('24h')
    expect(params.get('start')).toBeNull()
    expect(params.get('end')).toBeNull()
  })

  it('joins selectedServices into the services CSV param', () => {
    const url = buildExplorerUrl({
      selectedServices: ['docker:nginx', 'cron:backup'],
    })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('services')).toBe('docker:nginx,cron:backup')
  })

  it('omits the services param when selectedServices is empty', () => {
    const url = buildExplorerUrl({ sincePreset: '1h', selectedServices: [] })
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('services')).toBeNull()
  })

  it('returns /logs with no query string when no options produce params', () => {
    expect(buildExplorerUrl({})).toBe('/logs')
  })

  it('percent-encodes a logsQl with quotes and spaces and round-trips', () => {
    const logsQl = 'service:"home assistant"'
    const url = buildExplorerUrl({ logsQl })
    // The raw URL must be encoded (no literal space or unescaped quote in the
    // query string beyond what URLSearchParams produces).
    expect(url).toContain('logsql=')
    expect(url).not.toContain(' ') // no literal spaces in the path
    // Decoded round-trip recovers the original value.
    const params = new URLSearchParams(url.split('?')[1])
    expect(params.get('logsql')).toBe('service:"home assistant"')
  })

  it('every emitted key is within EXPLORER_URL_KEYS (exhaustive opts)', () => {
    const url = buildExplorerUrl({
      logsQl: 'service:"x"',
      selectedServices: ['docker:x'],
      rangeStart: new Date('2026-05-01T00:00:00.000Z'),
      rangeEnd: new Date('2026-05-01T01:00:00.000Z'),
    })
    const params = new URLSearchParams(url.split('?')[1])
    for (const key of params.keys()) {
      expect((EXPLORER_URL_KEYS as readonly string[]).includes(key)).toBe(true)
    }
  })

  it('EXPLORER_URL_KEYS is a subset of the /logs route accepted keys', () => {
    for (const key of EXPLORER_URL_KEYS) {
      expect((ROUTE_ACCEPTED_KEYS as readonly string[]).includes(key)).toBe(true)
    }
  })
})
