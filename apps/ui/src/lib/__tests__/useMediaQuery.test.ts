import { renderHook, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useMediaQuery } from '../useMediaQuery'

interface MockMql {
  matches: boolean
  media: string
  onchange: null
  addEventListener: (type: string, cb: (e: MediaQueryListEvent) => void) => void
  removeEventListener: (type: string, cb: (e: MediaQueryListEvent) => void) => void
  addListener: () => void
  removeListener: () => void
  dispatchEvent: () => boolean
  _fire: (matches: boolean) => void
}

function installMatchMedia(initial: boolean): MockMql {
  let listener: ((e: MediaQueryListEvent) => void) | null = null
  const mql: MockMql = {
    matches: initial,
    media: '',
    onchange: null,
    addEventListener: (_t, cb) => {
      listener = cb
    },
    removeEventListener: () => {
      listener = null
    },
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
    _fire: (matches: boolean) => {
      mql.matches = matches
      listener?.({ matches } as MediaQueryListEvent)
    },
  }
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => {
      mql.media = query
      return mql
    },
  })
  return mql
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useMediaQuery', () => {
  it('returns the initial match state', () => {
    installMatchMedia(true)
    const { result } = renderHook(() => useMediaQuery('(min-width: 640px)'))
    expect(result.current).toBe(true)
  })

  it('updates when the media query changes', () => {
    const mql = installMatchMedia(false)
    const { result } = renderHook(() => useMediaQuery('(min-width: 640px)'))
    expect(result.current).toBe(false)
    act(() => {
      mql._fire(true)
    })
    expect(result.current).toBe(true)
  })
})
