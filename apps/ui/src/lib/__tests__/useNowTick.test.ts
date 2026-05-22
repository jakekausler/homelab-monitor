import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useNowTick } from '../useNowTick'

describe('useNowTick', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-22T18:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns initial Date.now() value', () => {
    const { result } = renderHook(() => useNowTick(1000))
    expect(result.current).toBe(Date.parse('2026-05-22T18:00:00Z'))
  })

  it('updates after intervalMs', () => {
    const { result } = renderHook(() => useNowTick(1000))
    const initial = result.current
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(result.current).toBeGreaterThan(initial)
  })

  it('respects custom interval', () => {
    const { result } = renderHook(() => useNowTick(500))
    const initial = result.current
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(result.current).toBeGreaterThan(initial)
  })

  it('cleans up interval on unmount', () => {
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const { unmount } = renderHook(() => useNowTick(1000))
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })

  it('changes interval when intervalMs prop changes', () => {
    const { result, rerender } = renderHook(({ interval }) => useNowTick(interval), {
      initialProps: { interval: 1000 },
    })
    const initial = result.current
    rerender({ interval: 200 })
    act(() => {
      vi.advanceTimersByTime(200)
    })
    expect(result.current).toBeGreaterThan(initial)
  })
})
