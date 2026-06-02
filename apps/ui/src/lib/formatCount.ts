/**
 * Compact count formatter.
 *
 * Rules (truncate toward zero — never round up):
 *   < 1,000                  → integer string, e.g. "0", "42", "999"
 *   1,000–9,999              → one-decimal + 'k', truncated: "1.0k"–"9.9k"
 *   10,000–999,999           → integer + 'k': "10k"–"999k"
 *   1,000,000–9,999,999      → one-decimal + 'm': "1.0m"–"9.9m"
 *   10,000,000–999,999,999   → integer + 'm': "10m"–"999m"
 *   … same pattern for 'b', 't'
 *
 * Non-finite or negative inputs return String(n) (counts are always >= 0,
 * this is just a safety guard).
 */

const SUFFIXES = ['k', 'm', 'b', 't'] as const

export function formatCompactCount(n: number): string {
  if (!isFinite(n) || n < 0) return String(n)
  if (n < 1_000) return String(Math.floor(n))

  for (let i = 0; i < SUFFIXES.length; i++) {
    const base = Math.pow(1_000, i + 1) // 1e3, 1e6, 1e9, 1e12
    const nextBase = Math.pow(1_000, i + 2) // 1e6, 1e9, 1e12, 1e15

    if (n < nextBase || i === SUFFIXES.length - 1) {
      const suffix = SUFFIXES[i]
      if (n < base * 10) {
        // First decade of this magnitude tier → one decimal, truncated
        const truncated = Math.floor(n / (base / 10)) / 10
        return truncated.toFixed(1) + suffix
      } else {
        // Remaining decades → integer, truncated
        return String(Math.floor(n / base)) + suffix
      }
    }
  }

  // Unreachable with finite input
  return String(n)
}
