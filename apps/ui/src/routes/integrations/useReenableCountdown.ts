import { useEffect, useRef, useState } from 'react'

/**
 * Live per-second countdown for Pi-hole blocking auto-re-enable.
 * Server gives only a RELATIVE `blocking_timer_seconds` (per-30s-fetch snapshot);
 * we tick it down locally each second and re-anchor when the server value changes.
 *
 * NOTE: intentionally does NOT compose `@/lib/useNowTick`. This countdown must
 * re-anchor SYNCHRONOUSLY when the server `timerSeconds` changes (so the displayed
 * value jumps immediately to the new value on refetch, not one tick later). useNowTick
 * only exposes a delayed nowMs tick and cannot provide that synchronous re-anchor.
 * All impure/stateful work here is confined to the interval callback to stay lint-clean
 * under the react-hooks recommended-latest (React-Compiler purity) ruleset.
 *
 * Lint compliance (react-hooks recommended-latest):
 * - No setState in effect body (all setState happens in interval callback).
 * - Ref usage ONLY inside callbacks (never during render): deadlineRef holds
 *   the deadline for the current interval; updated via effect (post-render),
 *   read only by the tick callback (callback context is exempt).
 * The effect re-runs when `timerSeconds` changes, updating the deadline ref
 * and allowing the next tick to reflect the new countdown.
 */
export function useReenableCountdown(timerSeconds: number | null | undefined): number | null {
  const active = typeof timerSeconds === 'number' && timerSeconds > 0
  const [remaining, setRemaining] = useState<number | null>(() => (active ? timerSeconds : null))
  const deadlineRef = useRef<number | null>(null)

  useEffect(() => {
    if (!active) {
      deadlineRef.current = null
      return
    }
    // Update deadline ref on effect (after render) so interval callback reads fresh value.
    deadlineRef.current = Date.now() + timerSeconds * 1000
    const tick = (): void => {
      const deadline = deadlineRef.current
      if (deadline !== null) {
        setRemaining(Math.max(0, Math.round((deadline - Date.now()) / 1000)))
      }
    }
    // Tick immediately (synchronously) to update display after re-anchor.
    tick()
    const id = setInterval(tick, 1000)
    return () => {
      clearInterval(id)
    }
  }, [active, timerSeconds])

  return active ? remaining : null
}
