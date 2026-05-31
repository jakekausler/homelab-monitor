import { renderHook, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { useTimezonePreference } from '../useTimezonePreference'

const KEY = 'homelab-monitor:timezone'

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  window.localStorage.clear()
})

describe('useTimezonePreference', () => {
  it('defaults to local when nothing is stored', () => {
    const { result } = renderHook(() => useTimezonePreference())
    expect(result.current[0]).toBe('local')
  })

  it('reads a stored utc preference', () => {
    window.localStorage.setItem(KEY, 'utc')
    const { result } = renderHook(() => useTimezonePreference())
    expect(result.current[0]).toBe('utc')
  })

  it('falls back to local for an unrecognized stored value', () => {
    window.localStorage.setItem(KEY, 'banana')
    const { result } = renderHook(() => useTimezonePreference())
    expect(result.current[0]).toBe('local')
  })

  it('toggle flips the value and persists it', () => {
    const { result } = renderHook(() => useTimezonePreference())
    expect(result.current[0]).toBe('local')

    act(() => {
      result.current[1]()
    })
    expect(result.current[0]).toBe('utc')
    expect(window.localStorage.getItem(KEY)).toBe('utc')

    act(() => {
      result.current[1]()
    })
    expect(result.current[0]).toBe('local')
    expect(window.localStorage.getItem(KEY)).toBe('local')
  })

  it('persists the initial value to localStorage on mount', () => {
    renderHook(() => useTimezonePreference())
    expect(window.localStorage.getItem(KEY)).toBe('local')
  })
})
