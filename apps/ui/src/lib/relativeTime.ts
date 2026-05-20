/**
 * Format a UTC ISO-8601 timestamp as "5 minutes ago" / "in 3 hours".
 *
 * Uses Intl.RelativeTimeFormat with locale 'en'. Returns "—" when
 * iso is null/undefined/empty. Returns the raw absolute when more than
 * 30 days in either direction (relative time becomes meaningless).
 */
const RTF = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
const UNITS: Array<{ unit: Intl.RelativeTimeFormatUnit; ms: number }> = [
  { unit: 'year', ms: 365 * 24 * 60 * 60 * 1000 },
  { unit: 'month', ms: 30 * 24 * 60 * 60 * 1000 },
  { unit: 'day', ms: 24 * 60 * 60 * 1000 },
  { unit: 'hour', ms: 60 * 60 * 1000 },
  { unit: 'minute', ms: 60 * 1000 },
  { unit: 'second', ms: 1000 },
]

export function formatRelative(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const deltaMs = t - nowMs
  const abs = Math.abs(deltaMs)
  if (abs > 30 * 24 * 60 * 60 * 1000) {
    return new Date(t).toLocaleString()
  }
  for (const { unit, ms } of UNITS) {
    if (abs >= ms || unit === 'second') {
      const value = Math.round(deltaMs / ms)
      return RTF.format(value, unit)
    }
  }
  return RTF.format(0, 'second')
}

export function formatAbsolute(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

/**
 * Format a duration in seconds as a compact human-readable string.
 *
 * Returns "—" for null. Under 10s shows 3 decimals (e.g. "1.234s").
 * 10–59s shows 1 decimal (e.g. "10.0s"). 60–3599s shows "Xm Ys". 3600+ shows
 * "Xh Ym". Negative inputs are treated as 0.
 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '—'
  const s = Math.max(0, seconds)
  if (s < 10) return `${s.toFixed(3)}s`
  if (s < 60) return `${s.toFixed(1)}s`
  if (s < 3600) {
    const m = Math.floor(s / 60)
    const r = Math.floor(s % 60)
    return `${m}m ${r}s`
  }
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m`
}
