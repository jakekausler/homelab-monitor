// Project test conventions discovered:
// - Framework: Vitest with vi.mock()
// - localStorage mocking: clear in beforeEach/afterEach
// - Async: not needed for pure module tests
// - Style mirrors queryHistory.test.ts

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  EXPLORER_STATE_TTL_MS,
  loadExplorerState,
  patchExplorerState,
  resolveInitialExplorerState,
  saveExplorerState,
  STORAGE_KEY,
  type ExplorerState,
  type ExplorerSeed,
  type ExplorerUrlParams,
} from '../explorerState'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NOW = Date.now()

function makeState(overrides?: Partial<ExplorerState>): ExplorerState {
  return {
    advanced_mode: false,
    logs_ql: 'test query',
    selected_services: [{ service: 'home-assistant', source_type: 'docker' }],
    since_preset: '1h',
    range_start_iso: null,
    range_end_iso: null,
    scroll_position: null,
    cursor: null,
    last_visited_at: NOW,
    ...overrides,
  }
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  window.localStorage.clear()
})

// ---------------------------------------------------------------------------
// saveExplorerState / loadExplorerState round-trip
// ---------------------------------------------------------------------------

describe('saveExplorerState / loadExplorerState', () => {
  it('round-trip: save then load returns equivalent state', () => {
    const state = makeState()
    saveExplorerState(state)
    const loaded = loadExplorerState()
    expect(loaded).toEqual(state)
  })

  it('returns null when nothing stored', () => {
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for corrupt JSON', () => {
    window.localStorage.setItem(STORAGE_KEY, '{not valid json')
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for JSON that is not an object', () => {
    window.localStorage.setItem(STORAGE_KEY, '"string"')
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for JSON that is null', () => {
    window.localStorage.setItem(STORAGE_KEY, 'null')
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an array (not an object)', () => {
    window.localStorage.setItem(STORAGE_KEY, '[]')
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an object missing advanced_mode', () => {
    const bad = { logs_ql: 'x', selected_services: [], last_visited_at: NOW }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bad))
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an object missing logs_ql', () => {
    const bad = { advanced_mode: false, selected_services: [], last_visited_at: NOW }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bad))
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an object missing selected_services', () => {
    const bad = { advanced_mode: false, logs_ql: 'x', last_visited_at: NOW }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bad))
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an object with selected_services that is not an array', () => {
    const bad = {
      advanced_mode: false,
      logs_ql: 'x',
      selected_services: 'not-array',
      last_visited_at: NOW,
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bad))
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for an object missing last_visited_at', () => {
    const bad = { advanced_mode: false, logs_ql: 'x', selected_services: [] }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bad))
    expect(loadExplorerState()).toBeNull()
  })

  it('returns state for fresh last_visited_at (< 7 days old)', () => {
    const state = makeState({ last_visited_at: NOW - 1000 }) // 1 second ago
    saveExplorerState(state)
    expect(loadExplorerState()).toEqual(state)
  })

  it('returns state for last_visited_at exactly at the boundary (6 days 23h)', () => {
    const justFresh = NOW - (EXPLORER_STATE_TTL_MS - 60_000) // 1 minute before expiry
    const state = makeState({ last_visited_at: justFresh })
    saveExplorerState(state)
    expect(loadExplorerState()).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// TTL
// ---------------------------------------------------------------------------

describe('loadExplorerState TTL', () => {
  it('returns null for last_visited_at exactly 8 days ago (expired)', () => {
    const eightDaysAgo = NOW - 8 * 24 * 60 * 60 * 1000
    const state = makeState({ last_visited_at: eightDaysAgo })
    saveExplorerState(state)
    expect(loadExplorerState()).toBeNull()
  })

  it('returns null for last_visited_at exactly 7 days + 1ms ago (expired)', () => {
    const justExpired = NOW - (EXPLORER_STATE_TTL_MS + 1)
    const state = makeState({ last_visited_at: justExpired })
    saveExplorerState(state)
    expect(loadExplorerState()).toBeNull()
  })

  it('returns state for last_visited_at 1ms before the expiry boundary', () => {
    // TTL check is Date.now() - last_visited_at > TTL_MS (strict >).
    // Using NOW - TTL_MS + 500ms to stay safely within the fresh window even
    // accounting for a few ms of test execution time.
    const justFresh = NOW - EXPLORER_STATE_TTL_MS + 500
    const state = makeState({ last_visited_at: justFresh })
    saveExplorerState(state)
    expect(loadExplorerState()).toEqual(state)
  })
})

// ---------------------------------------------------------------------------
// patchExplorerState
// ---------------------------------------------------------------------------

describe('patchExplorerState', () => {
  it('creates a new object when no prior state exists', () => {
    patchExplorerState({ advanced_mode: false, logs_ql: 'new', selected_services: [] })
    const loaded = loadExplorerState()
    expect(loaded).not.toBeNull()
    expect(loaded?.logs_ql).toBe('new')
    expect(loaded?.advanced_mode).toBe(false)
    expect(typeof loaded?.last_visited_at).toBe('number')
  })

  it('preserves existing fields when patching a subset', () => {
    // First writer: query fields
    patchExplorerState({
      advanced_mode: false,
      logs_ql: 'original query',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    // Second writer: scroll_position only — must not clobber query fields
    patchExplorerState({ scroll_position: 300 })
    const loaded = loadExplorerState()
    expect(loaded?.logs_ql).toBe('original query')
    expect(loaded?.selected_services).toEqual([{ service: 'nginx', source_type: 'docker' }])
    expect(loaded?.scroll_position).toBe(300)
  })

  it('TWO-WRITER RACE: query fields then scroll — both preserved', () => {
    patchExplorerState({
      advanced_mode: false,
      logs_ql: 'race query',
      selected_services: [],
      since_preset: '24h',
    })
    patchExplorerState({ scroll_position: 1500 })
    const loaded = loadExplorerState()
    expect(loaded?.logs_ql).toBe('race query')
    expect(loaded?.since_preset).toBe('24h')
    expect(loaded?.scroll_position).toBe(1500)
  })

  it('TWO-WRITER RACE: scroll then query fields — both preserved', () => {
    // Seed an initial full state first so patchExplorerState has a base
    saveExplorerState(makeState({ logs_ql: 'initial', scroll_position: 0 }))
    patchExplorerState({ scroll_position: 750 })
    patchExplorerState({
      advanced_mode: false,
      logs_ql: 'updated query',
      selected_services: [{ service: 'app', source_type: 'docker' }],
    })
    const loaded = loadExplorerState()
    expect(loaded?.scroll_position).toBe(750)
    expect(loaded?.logs_ql).toBe('updated query')
  })

  it('updates last_visited_at on every patch', () => {
    const before = Date.now()
    patchExplorerState({ advanced_mode: false, logs_ql: 'q', selected_services: [] })
    const loaded = loadExplorerState()
    const after = Date.now()
    expect(loaded?.last_visited_at).toBeGreaterThanOrEqual(before)
    expect(loaded?.last_visited_at).toBeLessThanOrEqual(after)
  })

  it('overwrites prior query fields with a later patch', () => {
    patchExplorerState({
      advanced_mode: false,
      logs_ql: 'old',
      selected_services: [],
    })
    patchExplorerState({
      advanced_mode: true,
      logs_ql: 'new-logsql',
      selected_services: [{ service: 'foo', source_type: 'cron' }],
    })
    const loaded = loadExplorerState()
    expect(loaded?.advanced_mode).toBe(true)
    expect(loaded?.logs_ql).toBe('new-logsql')
  })

  it('treats expired previous state as absent (starts fresh)', () => {
    // Write an expired state directly
    const expired = makeState({ last_visited_at: NOW - 8 * 24 * 60 * 60 * 1000 })
    saveExplorerState(expired)
    // patch should not carry over the expired state's scroll_position
    patchExplorerState({ advanced_mode: false, logs_ql: 'fresh', selected_services: [] })
    const loaded = loadExplorerState()
    expect(loaded?.logs_ql).toBe('fresh')
    // scroll_position should be undefined or null — not carried from expired state
    expect(loaded?.scroll_position ?? null).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// resolveInitialExplorerState — URL precedence (ALL-OR-NOTHING)
// ---------------------------------------------------------------------------

describe('resolveInitialExplorerState', () => {
  const emptyUrl: ExplorerUrlParams = {}

  // Helper to assert default empty seed
  function expectDefaultSeed(seed: ExplorerSeed): void {
    expect(seed.advancedMode).toBe(false)
    expect(seed.plainText).toBe('')
    expect(seed.logsQl).toBe('')
    expect(seed.range).toEqual({ kind: 'preset', token: '1h' })
    expect(seed.selectedIdentities).toEqual([])
    expect(seed.restoreScrollTarget).toBeNull()
  }

  describe('Case 1: URL has params → URL wins, persisted ignored', () => {
    it('URL q param → seed has plain text, no restoreScrollTarget', () => {
      const persisted = makeState({ logs_ql: 'persisted query', scroll_position: 500 })
      const seed = resolveInitialExplorerState({ q: 'url query' }, persisted)
      expect(seed.plainText).toBe('url query')
      expect(seed.advancedMode).toBe(false)
      expect(seed.restoreScrollTarget).toBeNull()
    })

    it('URL logsql param → advanced mode, logsQl set', () => {
      const persisted = makeState({ logs_ql: 'ignored', scroll_position: 200 })
      const seed = resolveInitialExplorerState({ logsql: 'service:foo' }, persisted)
      expect(seed.advancedMode).toBe(true)
      expect(seed.logsQl).toBe('service:foo')
      expect(seed.plainText).toBe('')
      expect(seed.restoreScrollTarget).toBeNull()
    })

    it('URL since param → preset range from URL', () => {
      const seed = resolveInitialExplorerState({ since: '6h' }, null)
      expect(seed.range).toEqual({ kind: 'preset', token: '6h' })
    })

    it('URL start + end params → custom range', () => {
      const start = '2026-01-01T00:00:00.000Z'
      const end = '2026-01-02T00:00:00.000Z'
      const seed = resolveInitialExplorerState({ start, end }, null)
      expect(seed.range.kind).toBe('custom')
      if (seed.range.kind === 'custom') {
        expect(seed.range.start).toBeInstanceOf(Date)
        expect(seed.range.end).toBeInstanceOf(Date)
        expect(seed.range.start?.toISOString()).toBe(start)
        expect(seed.range.end?.toISOString()).toBe(end)
      }
    })

    it('URL services param → selectedIdentities from URL', () => {
      const services = [{ service: 'nginx', source_type: 'docker' }]
      const persisted = makeState({
        selected_services: [{ service: 'other', source_type: 'cron' }],
      })
      const seed = resolveInitialExplorerState({ services }, persisted)
      expect(seed.selectedIdentities).toEqual(services)
    })

    it('URL params present → persisted scroll is ignored (restoreScrollTarget null)', () => {
      const persisted = makeState({ scroll_position: 9999 })
      const seed = resolveInitialExplorerState({ q: 'anything' }, persisted)
      expect(seed.restoreScrollTarget).toBeNull()
    })

    it('empty services array does NOT count as URL-has-params', () => {
      // services: [] should not trigger the URL path
      const persisted = makeState({ logs_ql: 'from-persisted', scroll_position: 100 })
      const seed = resolveInitialExplorerState({ services: [] }, persisted)
      // Should fall through to persisted
      expect(seed.plainText).toBe('from-persisted')
    })
  })

  describe('Case 2: No URL params + fresh persisted → seed from persisted', () => {
    it('plain mode: plainText from logs_ql, logsQl empty', () => {
      const persisted = makeState({ advanced_mode: false, logs_ql: 'plain query' })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.advancedMode).toBe(false)
      expect(seed.plainText).toBe('plain query')
      expect(seed.logsQl).toBe('')
    })

    it('advanced mode: logsQl from logs_ql, plainText empty', () => {
      const persisted = makeState({ advanced_mode: true, logs_ql: 'service:foo' })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.advancedMode).toBe(true)
      expect(seed.logsQl).toBe('service:foo')
      expect(seed.plainText).toBe('')
    })

    it('selectedIdentities from persisted selected_services', () => {
      const services = [
        { service: 'home-assistant', source_type: 'docker' },
        { service: 'nginx', source_type: 'docker' },
      ]
      const persisted = makeState({ selected_services: services })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.selectedIdentities).toEqual(services)
    })

    it('preset range from persisted since_preset', () => {
      const persisted = makeState({
        since_preset: '24h',
        range_start_iso: null,
        range_end_iso: null,
      })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.range).toEqual({ kind: 'preset', token: '24h' })
    })

    it('custom range from persisted range_start_iso + range_end_iso', () => {
      const start = '2026-05-01T00:00:00.000Z'
      const end = '2026-05-02T00:00:00.000Z'
      const persisted = makeState({
        since_preset: null,
        range_start_iso: start,
        range_end_iso: end,
      })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.range.kind).toBe('custom')
      if (seed.range.kind === 'custom') {
        expect(seed.range.start?.toISOString()).toBe(start)
        expect(seed.range.end?.toISOString()).toBe(end)
      }
    })

    it('restoreScrollTarget is scroll_position when > 0', () => {
      const persisted = makeState({ scroll_position: 450 })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.restoreScrollTarget).toBe(450)
    })

    it('restoreScrollTarget is null when scroll_position is 0', () => {
      const persisted = makeState({ scroll_position: 0 })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.restoreScrollTarget).toBeNull()
    })

    it('restoreScrollTarget is null when scroll_position is null', () => {
      const persisted = makeState({ scroll_position: null })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.restoreScrollTarget).toBeNull()
    })

    it('restoreScrollTarget is null when scroll_position is negative (edge case)', () => {
      const persisted = makeState({ scroll_position: -1 })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      // scroll > 0 check: -1 is not > 0 so null
      expect(seed.restoreScrollTarget).toBeNull()
    })
  })

  describe('Case 3: No URL params + null persisted → default empty seed', () => {
    it('no URL, null persisted → default seed', () => {
      const seed = resolveInitialExplorerState(emptyUrl, null)
      expectDefaultSeed(seed)
    })

    it('no URL, expired persisted passed as null → default seed', () => {
      // Caller is responsible for passing loadExplorerState() (TTL-checked); we simulate
      // the expired case by passing null (what loadExplorerState() returns when expired).
      const seed = resolveInitialExplorerState(emptyUrl, null)
      expectDefaultSeed(seed)
    })
  })

  describe('range reconstruction edge cases', () => {
    it('unknown since_preset (not a valid PresetToken) → falls back to default 1h', () => {
      const persisted = makeState({
        since_preset: 'not-a-preset',
        range_start_iso: null,
        range_end_iso: null,
      })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.range).toEqual({ kind: 'preset', token: '1h' })
    })

    it('null since_preset + null custom bounds → default preset 1h', () => {
      const persisted = makeState({
        since_preset: null,
        range_start_iso: null,
        range_end_iso: null,
      })
      const seed = resolveInitialExplorerState(emptyUrl, persisted)
      expect(seed.range).toEqual({ kind: 'preset', token: '1h' })
    })

    it('URL since param with unknown token → default preset 1h', () => {
      const seed = resolveInitialExplorerState({ since: 'bogus' }, null)
      expect(seed.range).toEqual({ kind: 'preset', token: '1h' })
    })

    it('URL since param with valid preset 5m → preset 5m', () => {
      const seed = resolveInitialExplorerState({ since: '5m' }, null)
      expect(seed.range).toEqual({ kind: 'preset', token: '5m' })
    })

    it('URL since param with valid preset 7d → preset 7d', () => {
      const seed = resolveInitialExplorerState({ since: '7d' }, null)
      expect(seed.range).toEqual({ kind: 'preset', token: '7d' })
    })

    it('URL start only (no end) → custom range with only start', () => {
      const start = '2026-03-01T12:00:00.000Z'
      const seed = resolveInitialExplorerState({ start }, null)
      expect(seed.range.kind).toBe('custom')
      if (seed.range.kind === 'custom') {
        expect(seed.range.start).toBeInstanceOf(Date)
        expect(seed.range.end).toBeUndefined()
      }
    })

    it('URL end only (no start) → custom range with only end', () => {
      const end = '2026-03-02T12:00:00.000Z'
      const seed = resolveInitialExplorerState({ end }, null)
      expect(seed.range.kind).toBe('custom')
      if (seed.range.kind === 'custom') {
        expect(seed.range.end).toBeInstanceOf(Date)
        expect(seed.range.start).toBeUndefined()
      }
    })

    it('URL q empty string → still counts as urlHasAny (q !== undefined)', () => {
      const persisted = makeState({ logs_ql: 'ignored' })
      const seed = resolveInitialExplorerState({ q: '' }, persisted)
      // URL had q defined (even if empty string) → URL path taken
      expect(seed.plainText).toBe('')
      expect(seed.restoreScrollTarget).toBeNull()
    })
  })
})
