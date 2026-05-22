const MS_PER_SECOND = 1000
const MS_PER_MINUTE = 60 * MS_PER_SECOND
const MS_PER_HOUR = 60 * MS_PER_MINUTE
const MS_PER_DAY = 24 * MS_PER_HOUR
const THREE_DAYS_MS = 3 * MS_PER_DAY

/**
 * Format a UTC ISO-8601 timestamp as a dual-unit relative string.
 *
 * Stepdown precision:
 * - ≤ 60s: "Xs ago" / "in Xs"
 * - < 1h: "Xm Ys ago" / "in Xm Ys"
 * - < 24h: "Xh Ym ago" / "in Xh Ym"
 * - 1-3 days: "Xd Yh ago" / "in Xd Yh"
 * - > 3 days: "Xd ago" / "in Xd"
 *
 * Returns "—" when ``iso`` is null/undefined/empty. Returns "just now"
 * when |delta| < 1s. Returns the raw value when ``iso`` is not a valid
 * timestamp.
 */
export function formatRelative(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (iso === null || iso === undefined || iso === '') return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const deltaMs = t - nowMs
  const abs = Math.abs(deltaMs)
  if (abs < MS_PER_SECOND) return 'just now'
  const past = deltaMs < 0
  const text = _formatDelta(abs)
  return past ? `${text} ago` : `in ${text}`
}

function _formatDelta(abs: number): string {
  if (abs <= MS_PER_MINUTE) {
    const s = Math.round(abs / MS_PER_SECOND)
    return `${s}s`
  }
  if (abs < MS_PER_HOUR) {
    const m = Math.floor(abs / MS_PER_MINUTE)
    const s = Math.floor((abs % MS_PER_MINUTE) / MS_PER_SECOND)
    return `${m}m ${s}s`
  }
  if (abs < MS_PER_DAY) {
    const h = Math.floor(abs / MS_PER_HOUR)
    const m = Math.floor((abs % MS_PER_HOUR) / MS_PER_MINUTE)
    return `${h}h ${m}m`
  }
  if (abs <= THREE_DAYS_MS) {
    const d = Math.floor(abs / MS_PER_DAY)
    const h = Math.floor((abs % MS_PER_DAY) / MS_PER_HOUR)
    return `${d}d ${h}h`
  }
  const d = Math.floor(abs / MS_PER_DAY)
  return `${d}d`
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
