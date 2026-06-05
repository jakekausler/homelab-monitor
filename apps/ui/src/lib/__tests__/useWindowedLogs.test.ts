import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import type { LogLine } from '@/components/logs/types'
import {
  useWindowedLogs,
  windowedLogsReducer,
  RENDER_CAP,
  type WindowedLogsState,
  type WindowedLogsAction,
} from '../useWindowedLogs'

function mk(message: string, ts = '2000-01-01T00:00:00Z'): LogLine {
  return {
    timestamp: ts,
    message,
    stream: 'stdout',
    severity: null,
    host: null,
    service: null,
    fields: {},
  }
}

describe('windowedLogsReducer', () => {
  let initialState: WindowedLogsState

  beforeEach(() => {
    initialState = { lines: [], trimmedOlder: false, trimmedNewer: false }
  })

  describe('reset', () => {
    it('replaces lines and clears both flags', () => {
      const state = { lines: [mk('a'), mk('b')], trimmedOlder: true, trimmedNewer: true }
      const action: WindowedLogsAction = { type: 'reset', lines: [mk('c'), mk('d')] }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(2)
      expect(result.lines[0]?.message).toBe('c')
      expect(result.lines[1]?.message).toBe('d')
      expect(result.trimmedOlder).toBe(false)
      expect(result.trimmedNewer).toBe(false)
    })

    it('over cap keeps LAST CAP, sets trimmedOlder=true, trimmedNewer=false', () => {
      const lines = Array.from({ length: RENDER_CAP + 5 }, (_, i) => mk(`m${i}`))
      const action: WindowedLogsAction = { type: 'reset', lines }
      const result = windowedLogsReducer(initialState, action)

      expect(result.lines).toHaveLength(RENDER_CAP)
      expect(result.lines[0]?.message).toBe('m5')
      expect(result.trimmedOlder).toBe(true)
      expect(result.trimmedNewer).toBe(false)
    })
  })

  describe('prependOlder', () => {
    it('under cap keeps lines prepended in order, trimmedOlder=false, preserves trimmedNewer', () => {
      const state: WindowedLogsState = {
        lines: [mk('b'), mk('c')],
        trimmedOlder: true,
        trimmedNewer: true,
      }
      const action: WindowedLogsAction = { type: 'prependOlder', lines: [mk('a')] }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(3)
      expect(result.lines[0]?.message).toBe('a')
      expect(result.lines[1]?.message).toBe('b')
      expect(result.lines[2]?.message).toBe('c')
      expect(result.trimmedOlder).toBe(false)
      expect(result.trimmedNewer).toBe(true)
    })

    it('over cap keeps FIRST CAP, sets trimmedNewer=true, trimmedOlder=false', () => {
      const state: WindowedLogsState = { lines: [], trimmedOlder: false, trimmedNewer: false }
      const olderLines = Array.from({ length: RENDER_CAP + 100 }, (_, i) =>
        mk(`older${i}`, `2000-01-01T00:${i < 10 ? 0 : 1}:00Z`),
      )
      const action: WindowedLogsAction = { type: 'prependOlder', lines: olderLines }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(RENDER_CAP)
      expect(result.lines[0]?.message).toBe('older0')
      expect(result.trimmedOlder).toBe(false)
      expect(result.trimmedNewer).toBe(true)
    })

    it('empty input clears trimmedOlder, lines unchanged', () => {
      const state: WindowedLogsState = {
        lines: [mk('a'), mk('b')],
        trimmedOlder: true,
        trimmedNewer: true,
      }
      const action: WindowedLogsAction = { type: 'prependOlder', lines: [] }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(2)
      expect(result.lines[0]?.message).toBe('a')
      expect(result.trimmedOlder).toBe(false)
      expect(result.trimmedNewer).toBe(true)
    })
  })

  describe('appendNewer', () => {
    it('under cap appends lines, trimmedNewer=false, preserves trimmedOlder', () => {
      const state: WindowedLogsState = {
        lines: [mk('a'), mk('b')],
        trimmedOlder: true,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = { type: 'appendNewer', lines: [mk('c'), mk('d')] }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(4)
      expect(result.lines[2]?.message).toBe('c')
      expect(result.lines[3]?.message).toBe('d')
      expect(result.trimmedOlder).toBe(true)
      expect(result.trimmedNewer).toBe(false)
    })

    it('over cap keeps LAST CAP, sets trimmedOlder=true, trimmedNewer=false', () => {
      const lines = Array.from({ length: 100 }, (_, i) => mk(`m${i}`))
      const state: WindowedLogsState = {
        lines,
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const newLines = Array.from({ length: RENDER_CAP + 50 }, (_, i) => mk(`new${i}`))
      const action: WindowedLogsAction = { type: 'appendNewer', lines: newLines }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(RENDER_CAP)
      expect(result.lines[0]?.message).toMatch(/^new/)
      expect(result.trimmedOlder).toBe(true)
      expect(result.trimmedNewer).toBe(false)
    })

    it('dedupes boundary line (same key as last existing line)', () => {
      const shared = mk('shared', '2000-01-01T00:00:00Z')
      const state: WindowedLogsState = {
        lines: [mk('a'), shared],
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = {
        type: 'appendNewer',
        lines: [shared, mk('c')],
      }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(3)
      expect(result.lines[0]?.message).toBe('a')
      expect(result.lines[1]?.message).toBe('shared')
      expect(result.lines[2]?.message).toBe('c')
    })

    it('keeps distinct lines with same timestamp but different message', () => {
      const ts = '2000-01-01T00:00:00Z'
      const state: WindowedLogsState = {
        lines: [mk('msg1', ts)],
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = {
        type: 'appendNewer',
        lines: [mk('msg2', ts), mk('msg3', ts)],
      }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(3)
      expect(result.lines[1]?.message).toBe('msg2')
      expect(result.lines[2]?.message).toBe('msg3')
    })

    it('all-duplicate batch returns same state reference', () => {
      const shared = mk('shared', '2000-01-01T00:00:00Z')
      const state: WindowedLogsState = {
        lines: [mk('a'), shared],
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = {
        type: 'appendNewer',
        lines: [shared],
      }
      const result = windowedLogsReducer(state, action)

      expect(result).toBe(state)
    })
  })

  describe('trimFrontTo', () => {
    it('drops to last n, sets trimmedOlder=true when lines.length > n', () => {
      const state: WindowedLogsState = {
        lines: [mk('a'), mk('b'), mk('c'), mk('d')],
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = { type: 'trimFrontTo', n: 2 }
      const result = windowedLogsReducer(state, action)

      expect(result.lines).toHaveLength(2)
      expect(result.lines[0]?.message).toBe('c')
      expect(result.lines[1]?.message).toBe('d')
      expect(result.trimmedOlder).toBe(true)
    })

    it('no-op (same reference) when lines.length <= n', () => {
      const state: WindowedLogsState = {
        lines: [mk('a'), mk('b')],
        trimmedOlder: false,
        trimmedNewer: false,
      }
      const action: WindowedLogsAction = { type: 'trimFrontTo', n: 3 }
      const result = windowedLogsReducer(state, action)

      expect(result).toBe(state)
    })
  })
})

describe('useWindowedLogs', () => {
  it('initial state is empty with both flags false', () => {
    const { result } = renderHook(() => useWindowedLogs())

    expect(result.current.state.lines).toHaveLength(0)
    expect(result.current.state.trimmedOlder).toBe(false)
    expect(result.current.state.trimmedNewer).toBe(false)
  })

  it('reset updates state.lines', () => {
    const { result } = renderHook(() => useWindowedLogs())

    act(() => {
      result.current.reset([mk('a'), mk('b')])
    })

    expect(result.current.state.lines).toHaveLength(2)
    expect(result.current.state.lines[0]?.message).toBe('a')
  })

  it('callbacks are stable across rerenders', () => {
    const { result, rerender } = renderHook(() => useWindowedLogs())

    const { reset: reset1, prependOlder: po1, appendNewer: an1, trimFrontTo: tf1 } = result.current

    rerender()

    const { reset: reset2, prependOlder: po2, appendNewer: an2, trimFrontTo: tf2 } = result.current

    expect(reset1).toBe(reset2)
    expect(po1).toBe(po2)
    expect(an1).toBe(an2)
    expect(tf1).toBe(tf2)
  })
})
