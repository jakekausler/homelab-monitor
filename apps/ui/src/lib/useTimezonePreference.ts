import { useCallback, useEffect, useState } from 'react'

const TIMEZONE_STORAGE_KEY = 'homelab-monitor:timezone'

/** UTC vs the configured local display zone. */
export type TimezonePreference = 'local' | 'utc'

/**
 * Read the persisted timezone preference. SSR-safe. Any unrecognized stored
 * value falls back to the default ('local'). Mirrors AppShell.readInitialTheme.
 */
function readInitialTimezone(): TimezonePreference {
  if (typeof window === 'undefined') return 'local'
  const stored = window.localStorage.getItem(TIMEZONE_STORAGE_KEY)
  return stored === 'utc' ? 'utc' : 'local'
}

/**
 * Persisted UTC/local timestamp preference for the log viewers.
 *
 * Returns a tuple: the current preference and a toggle that flips it.
 * Default 'local'. Persists to localStorage on change. STAGE-004-009.
 */
export function useTimezonePreference(): [TimezonePreference, () => void] {
  const [timezone, setTimezone] = useState<TimezonePreference>(readInitialTimezone)

  const toggleTimezone = useCallback(() => {
    setTimezone((t) => (t === 'utc' ? 'local' : 'utc'))
  }, [])

  // Write-back only — does NOT call setTimezone (no set-state-in-effect).
  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(TIMEZONE_STORAGE_KEY, timezone)
  }, [timezone])

  return [timezone, toggleTimezone]
}
