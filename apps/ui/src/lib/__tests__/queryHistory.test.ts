import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  appendWithDedupeAndCap,
  clearHistory,
  equalityKey,
  readHistory,
  recordQuery,
  STORAGE_KEY,
  subscribe,
  writeHistory,
  type HistoryEntry,
} from '../queryHistory'

const makeEntry = (overrides?: Partial<HistoryEntry>): HistoryEntry => ({
  id: 'test-id',
  timestamp: 1000,
  advanced_mode: false,
  logs_ql: 'test query',
  selected_services: [{ service: 'svc1', source_type: 'docker' }],
  since_preset: '1h',
  ...overrides,
})

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  window.localStorage.clear()
})

describe('appendWithDedupeAndCap', () => {
  it('appends to an empty list', () => {
    const entry = makeEntry()
    const result = appendWithDedupeAndCap([], entry)
    expect(result).toHaveLength(1)
    expect(result[0]).toEqual(entry)
  })

  it('prepends a distinct entry', () => {
    const first = makeEntry({ id: 'id-1', timestamp: 1000, logs_ql: 'query 1' })
    const second = makeEntry({ id: 'id-2', timestamp: 2000, logs_ql: 'query 2' })
    const result = appendWithDedupeAndCap([first], second)
    expect(result).toHaveLength(2)
    expect(result[0]).toEqual(second)
    expect(result[1]).toEqual(first)
  })

  it('caps at 20 entries (oldest rolls off)', () => {
    const entries = Array.from({ length: 20 }, (_, i) =>
      makeEntry({ id: `id-${i}`, timestamp: 19 - i, logs_ql: `query ${19 - i}` }),
    )
    const newEntry = makeEntry({ id: 'id-new', timestamp: 21, logs_ql: 'query 21' })
    const result = appendWithDedupeAndCap(entries, newEntry)
    expect(result).toHaveLength(20)
    expect(result[0]).toEqual(newEntry)
    // The oldest (first inserted, now at index 19) is gone
    expect(result.map((e) => e.logs_ql)).not.toContain('query 0')
  })

  it('collapses consecutive identical entries (updates timestamp only)', () => {
    const first = makeEntry({ id: 'id-1', timestamp: 1000, logs_ql: 'same query' })
    const second = makeEntry({ id: 'id-2', timestamp: 2000, logs_ql: 'same query' })
    const result = appendWithDedupeAndCap([first], second)
    expect(result).toHaveLength(1)
    expect(result[0]?.id).toBe('id-1')
    expect(result[0]?.timestamp).toBe(2000)
    expect(result[0]?.logs_ql).toBe('same query')
  })

  it('does not collapse non-consecutive identical entries (A, B, then A again)', () => {
    const a = makeEntry({ id: 'id-a', timestamp: 1000, logs_ql: 'query A' })
    const b = makeEntry({ id: 'id-b', timestamp: 2000, logs_ql: 'query B' })
    const a2 = makeEntry({ id: 'id-a2', timestamp: 3000, logs_ql: 'query A' })
    const result = appendWithDedupeAndCap([b, a], a2)
    expect(result).toHaveLength(3)
    expect(result[0]).toEqual(a2)
  })
})

describe('equalityKey', () => {
  it('ignores timestamp', () => {
    const entry1 = makeEntry({ timestamp: 1000 })
    const entry2 = makeEntry({ timestamp: 2000 })
    expect(equalityKey(entry1)).toBe(equalityKey(entry2))
  })

  it('ignores resultCount', () => {
    const entry1 = makeEntry()
    const entry2 = makeEntry({ resultCount: 42 })
    expect(equalityKey(entry1)).toBe(equalityKey(entry2))
  })

  it('ignores id', () => {
    const entry1 = makeEntry({ id: 'id-1' })
    const entry2 = makeEntry({ id: 'id-2' })
    expect(equalityKey(entry1)).toBe(equalityKey(entry2))
  })

  it('is order-insensitive for selected_services', () => {
    const entry1 = makeEntry({
      selected_services: [
        { service: 'a', source_type: 'docker' },
        { service: 'b', source_type: 'k8s' },
      ],
    })
    const entry2 = makeEntry({
      selected_services: [
        { service: 'b', source_type: 'k8s' },
        { service: 'a', source_type: 'docker' },
      ],
    })
    expect(equalityKey(entry1)).toBe(equalityKey(entry2))
  })

  it('distinguishes logs_ql', () => {
    const entry1 = makeEntry({ logs_ql: 'query A' })
    const entry2 = makeEntry({ logs_ql: 'query B' })
    expect(equalityKey(entry1)).not.toBe(equalityKey(entry2))
  })

  it('distinguishes advanced_mode', () => {
    const entry1 = makeEntry({ advanced_mode: false })
    const entry2 = makeEntry({ advanced_mode: true })
    expect(equalityKey(entry1)).not.toBe(equalityKey(entry2))
  })

  it('distinguishes since_preset', () => {
    const entry1 = makeEntry({ since_preset: '1h', range_start_iso: null, range_end_iso: null })
    const entry2 = makeEntry({ since_preset: '24h', range_start_iso: null, range_end_iso: null })
    expect(equalityKey(entry1)).not.toBe(equalityKey(entry2))
  })

  it('distinguishes custom range bounds', () => {
    const entry1 = makeEntry({
      since_preset: null,
      range_start_iso: '2026-01-01T00:00:00Z',
      range_end_iso: '2026-01-02T00:00:00Z',
    })
    const entry2 = makeEntry({
      since_preset: null,
      range_start_iso: '2026-01-01T00:00:00Z',
      range_end_iso: '2026-01-03T00:00:00Z',
    })
    expect(equalityKey(entry1)).not.toBe(equalityKey(entry2))
  })
})

describe('readHistory', () => {
  it('returns [] when nothing is stored', () => {
    const result = readHistory()
    expect(result).toEqual([])
  })

  it('returns [] for corrupt JSON', () => {
    window.localStorage.setItem(STORAGE_KEY, '{not valid json')
    const result = readHistory()
    expect(result).toEqual([])
  })

  it('returns [] for non-array JSON', () => {
    window.localStorage.setItem(STORAGE_KEY, '"string"')
    const result = readHistory()
    expect(result).toEqual([])
  })

  it('returns [] when a non-array value is stored', () => {
    window.localStorage.setItem(STORAGE_KEY, '42')
    const result = readHistory()
    expect(result).toEqual([])
  })

  it('returns the stored array', () => {
    const entry = makeEntry()
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([entry]))
    const result = readHistory()
    expect(result).toHaveLength(1)
    expect(result[0]).toEqual(entry)
  })
})

describe('writeHistory', () => {
  it('persists entries to localStorage', () => {
    const entry = makeEntry()
    writeHistory([entry])
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '[]') as HistoryEntry[]
    expect(stored).toEqual([entry])
  })

  it('notifies subscribers', () => {
    const listener = vi.fn()
    subscribe(listener)
    writeHistory([makeEntry()])
    expect(listener).toHaveBeenCalled()
  })
})

describe('recordQuery', () => {
  it('reads, appends, writes, and notifies', () => {
    const listener = vi.fn()
    subscribe(listener)
    const entry1 = makeEntry({ id: 'id-1', timestamp: 1000, logs_ql: 'query 1' })
    const entry2 = makeEntry({ id: 'id-2', timestamp: 2000, logs_ql: 'query 2' })
    recordQuery(entry1)
    expect(listener).toHaveBeenCalledTimes(1)
    recordQuery(entry2)
    expect(listener).toHaveBeenCalledTimes(2)
    const stored = readHistory()
    expect(stored).toHaveLength(2)
    expect(stored[0]).toEqual(entry2)
  })
})

describe('clearHistory', () => {
  it('empties storage and notifies subscribers', () => {
    const listener = vi.fn()
    subscribe(listener)
    recordQuery(makeEntry())
    expect(readHistory()).toHaveLength(1)
    clearHistory()
    expect(readHistory()).toHaveLength(0)
    expect(listener).toHaveBeenCalledTimes(2) // once for recordQuery, once for clearHistory
  })
})

describe('subscribe', () => {
  it('returns an unsubscribe function that stops notifications', () => {
    const listener = vi.fn()
    const unsubscribe = subscribe(listener)
    recordQuery(makeEntry({ id: 'id-1', timestamp: 1000 }))
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
    recordQuery(makeEntry({ id: 'id-2', timestamp: 2000 }))
    expect(listener).toHaveBeenCalledTimes(1) // not called again
  })
})
