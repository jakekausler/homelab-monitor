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

const LOG_TS_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/

/**
 * Format a log line's UTC timestamp for display: drop sub-seconds and the
 * 'T', append ' UTC'. STILL UTC — no zone shift here.
 *
 * '2026-05-29T12:53:53.162712958Z' -> '2026-05-29 12:53:53 UTC'
 *
 * Implementation note: deliberately does NOT use Date parsing — Date would
 * zone-shift the display and truncate nanosecond precision unpredictably.
 * A regex extracts the date + HH:MM:SS and discards the fractional seconds
 * and any zone suffix.
 *
 * STAGE-004-009 will layer America/New_York (configurable) zone conversion
 * onto THIS exact helper — keep the signature and return shape stable.
 *
 * Returns `raw ?? ''` for null/undefined/empty, and the raw string unchanged
 * when it does not match the ISO-ish shape.
 */
export function formatLogTimestamp(raw: string | null | undefined): string {
  if (raw == null || raw === '') return raw ?? ''
  const match = LOG_TS_RE.exec(raw)
  if (match === null) return raw
  const date = match[1] ?? ''
  const time = match[2] ?? ''
  return `${date} ${time} UTC`
}
