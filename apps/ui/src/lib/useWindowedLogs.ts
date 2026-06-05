import { useCallback, useReducer } from 'react'

import type { LogLine } from '@/components/logs/types'

/** 1000-line render cap (FIFO). The single source of truth for rendered lines. */
export const RENDER_CAP = 1000

/** Number of existing tail-region lines hashed for incoming-batch dedup. */
const DEDUP_WINDOW = 500

export interface WindowedLogsState {
  lines: LogLine[]
  trimmedOlder: boolean
  trimmedNewer: boolean
}

export type WindowedLogsAction =
  | { type: 'reset'; lines: LogLine[] }
  | { type: 'prependOlder'; lines: LogLine[] }
  | { type: 'appendNewer'; lines: LogLine[] }
  | { type: 'trimFrontTo'; n: number }

const INITIAL: WindowedLogsState = {
  lines: [],
  trimmedOlder: false,
  trimmedNewer: false,
}

function lineKey(l: LogLine): string {
  return `${l.timestamp}|${l.message}|${l.severity ?? ''}|${l.host ?? ''}|${l.service ?? ''}`
}

/** Drop any incoming line whose composite key already exists in the tail window. */
function dedupeAgainst(existing: LogLine[], incoming: LogLine[]): LogLine[] {
  if (incoming.length === 0) return incoming
  const seen = new Set<string>()
  const start = Math.max(0, existing.length - DEDUP_WINDOW)
  for (let i = start; i < existing.length; i++) {
    const l = existing[i]
    if (l !== undefined) seen.add(lineKey(l))
  }
  return incoming.filter((l) => !seen.has(lineKey(l)))
}

export function windowedLogsReducer(
  state: WindowedLogsState,
  action: WindowedLogsAction,
): WindowedLogsState {
  switch (action.type) {
    case 'reset': {
      const over = action.lines.length > RENDER_CAP
      return {
        lines: over ? action.lines.slice(action.lines.length - RENDER_CAP) : action.lines,
        trimmedOlder: over,
        trimmedNewer: false,
      }
    }
    case 'prependOlder': {
      if (action.lines.length === 0) {
        return { ...state, trimmedOlder: false }
      }
      const next = [...action.lines, ...state.lines]
      const over = next.length > RENDER_CAP
      return {
        lines: over ? next.slice(0, RENDER_CAP) : next,
        trimmedOlder: false,
        trimmedNewer: over ? true : state.trimmedNewer,
      }
    }
    case 'appendNewer': {
      const deduped = dedupeAgainst(state.lines, action.lines)
      if (deduped.length === 0) return state
      const next = [...state.lines, ...deduped]
      const over = next.length > RENDER_CAP
      return {
        lines: over ? next.slice(next.length - RENDER_CAP) : next,
        trimmedOlder: over ? true : state.trimmedOlder,
        trimmedNewer: false,
      }
    }
    case 'trimFrontTo': {
      if (state.lines.length <= action.n) return state
      return {
        lines: state.lines.slice(state.lines.length - action.n),
        trimmedOlder: true,
        trimmedNewer: state.trimmedNewer,
      }
    }
    default:
      return state
  }
}

export interface UseWindowedLogsResult {
  state: WindowedLogsState
  reset: (lines: LogLine[]) => void
  prependOlder: (lines: LogLine[]) => void
  appendNewer: (lines: LogLine[]) => void
  trimFrontTo: (n: number) => void
}

export function useWindowedLogs(): UseWindowedLogsResult {
  const [state, dispatch] = useReducer(windowedLogsReducer, INITIAL)

  const reset = useCallback((lines: LogLine[]) => {
    dispatch({ type: 'reset', lines })
  }, [])
  const prependOlder = useCallback((lines: LogLine[]) => {
    dispatch({ type: 'prependOlder', lines })
  }, [])
  const appendNewer = useCallback((lines: LogLine[]) => {
    dispatch({ type: 'appendNewer', lines })
  }, [])
  const trimFrontTo = useCallback((n: number) => {
    dispatch({ type: 'trimFrontTo', n })
  }, [])

  return { state, reset, prependOlder, appendNewer, trimFrontTo }
}
