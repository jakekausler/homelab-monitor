import { useEffect, useState } from 'react'

/**
 * Returns a `Date.now()` value that re-renders the calling component every
 * `intervalMs` milliseconds. Use for relative-time displays so "5s ago"
 * tick-counts toward "6s ago" without depending on a server poll.
 *
 * The returned value is suitable for passing to `formatRelative(iso, nowMs)`
 * as its second argument. Defaults to 1s for human-readable ticking.
 *
 * Implementation notes:
 * - Uses setInterval (not requestAnimationFrame) — we don't need 60fps; we
 *   need stable 1s ticks that work when the tab is backgrounded (browsers
 *   throttle setInterval in background tabs to ~1Hz minimum, which matches
 *   our default cadence anyway).
 * - Returns the snapshot time, not a function — components can call
 *   formatRelative(iso, nowMs) directly.
 * - Pauses ticking when the tab is hidden (via visibilitychange event) to
 *   save CPU; resumes with an immediate update when the tab becomes visible.
 */
export function useNowTick(intervalMs: number = 1000): number {
  const [nowMs, setNowMs] = useState<number>(() => Date.now())
  useEffect(() => {
    let timerId: ReturnType<typeof setInterval> | null = null

    const start = () => {
      if (timerId !== null) return
      timerId = setInterval(() => {
        setNowMs(Date.now())
      }, intervalMs)
    }

    const stop = () => {
      if (timerId !== null) {
        clearInterval(timerId)
        timerId = null
      }
    }

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        // Catch up immediately on becoming visible
        setNowMs(Date.now())
        start()
      } else {
        stop()
      }
    }

    if (document.visibilityState === 'visible') {
      start()
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [intervalMs])
  return nowMs
}
