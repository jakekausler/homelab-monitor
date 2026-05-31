import { useEffect, useState } from 'react'

/**
 * Subscribe to a CSS media query. SSR-safe (returns false when window is
 * unavailable). Used by <TimeRangeControl> to pick popover (desktop) vs
 * full-screen dialog (mobile). STAGE-004-008.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return false
    }
    return window.matchMedia(query).matches
  })

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return
    }
    const mql = window.matchMedia(query)
    const handler = (e: MediaQueryListEvent): void => {
      setMatches(e.matches)
    }
    mql.addEventListener('change', handler)
    return () => {
      mql.removeEventListener('change', handler)
    }
  }, [query])

  return matches
}
