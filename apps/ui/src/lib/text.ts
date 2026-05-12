/**
 * Capitalize the first character of a string. Used for display-only
 * Title-Case-ish rendering of enum-shaped API values (e.g. `observe` → `Observe`).
 * The lowercase source value is preserved on `aria-label` for screen readers.
 */
export function capitalize(s: string): string {
  if (s.length === 0) return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}
