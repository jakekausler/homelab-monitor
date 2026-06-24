import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useReenableCountdown } from './useReenableCountdown'

describe('useReenableCountdown', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-22T18:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns null when timerSeconds is null', () => {
    const { result } = renderHook(() => useReenableCountdown(null))
    expect(result.current).toBe(null)
  })

  it('returns null when timerSeconds is undefined', () => {
    const { result } = renderHook(() => useReenableCountdown(undefined))
    expect(result.current).toBe(null)
  })

  it('returns null when timerSeconds is <= 0', () => {
    const { result } = renderHook(() => useReenableCountdown(0))
    expect(result.current).toBe(null)
    const { result: resultNeg } = renderHook(() => useReenableCountdown(-5))
    expect(resultNeg.current).toBe(null)
  })

  it('returns initial value equal to timerSeconds (floored)', () => {
    const { result } = renderHook(() => useReenableCountdown(3))
    expect(result.current).toBe(3)
  })

  it('decrements by 1 after each second', () => {
    const { result } = renderHook(() => useReenableCountdown(3))
    expect(result.current).toBe(3)
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(result.current).toBe(2)
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(result.current).toBe(1)
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(result.current).toBe(0)
  })

  it('floors at 0 and does not go negative', () => {
    const { result } = renderHook(() => useReenableCountdown(1))
    expect(result.current).toBe(1)
    act(() => {
      vi.advanceTimersByTime(5000)
    })
    expect(result.current).toBe(0)
  })

  it('re-anchors and jumps to new timerSeconds when arg changes', () => {
    const { result, rerender } = renderHook(({ seconds }) => useReenableCountdown(seconds), {
      initialProps: { seconds: 3 },
    })
    expect(result.current).toBe(3)
    // Change to a larger timer
    rerender({ seconds: 298 })
    // Should immediately reflect the new value
    expect(result.current).toBe(298)
  })

  it('returns null when rerendering with timerSeconds <= 0', () => {
    const { result, rerender } = renderHook(({ seconds }) => useReenableCountdown(seconds), {
      initialProps: { seconds: 10 },
    })
    expect(result.current).toBe(10)
    rerender({ seconds: 0 })
    expect(result.current).toBe(null)
  })

  it('cleans up interval on unmount', () => {
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const { unmount } = renderHook(() => useReenableCountdown(30))
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })
})
