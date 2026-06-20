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

const MIN = 60
const HR = 3600
const DAY = 86400
const WEEK = 604800
const YEAR = 31536000

/**
 * Format a device uptime in seconds as a two-unit human-readable string.
 * Tiers: Xy Yw | Xw Yd | Xd Yh | Xh Ym | Xm Ys | Xs | —
 */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.floor(seconds))
  if (s < MIN) return `${s}s`
  if (s < HR) {
    const m = Math.floor(s / MIN)
    const r = s % MIN
    return `${m}m ${r}s`
  }
  if (s < DAY) {
    const h = Math.floor(s / HR)
    const m = Math.floor((s % HR) / MIN)
    return `${h}h ${m}m`
  }
  if (s < WEEK) {
    const d = Math.floor(s / DAY)
    const h = Math.floor((s % DAY) / HR)
    return `${d}d ${h}h`
  }
  if (s < YEAR) {
    const w = Math.floor(s / WEEK)
    const d = Math.floor((s % WEEK) / DAY)
    return `${w}w ${d}d`
  }
  const y = Math.floor(s / YEAR)
  const w = Math.floor((s % YEAR) / WEEK)
  return `${y}y ${w}w`
}

/**
 * Coarse single-unit relative age with an " ago" suffix.
 * Thresholds: <60s → "just now"; <3600s → minutes; <86400s → hours; else days.
 * Negative inputs clamp to 0.
 */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

const LOG_TS_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/

/**
 * Default display timezone. The 'local' branch of formatLogTimestamp renders
 * the CONFIGURED zone (NOT the browser zone) via an explicit Intl timeZone.
 */
// configurable: future env var (YAGNI — single constant seam for now)
export const DEFAULT_DISPLAY_TZ = 'America/New_York'

/** Options for formatLogTimestamp / formatLogTimestampParts. */
export interface LogTimestampOptions {
  timezone?: 'local' | 'utc'
  tz?: string
}

/**
 * Format a log line's UTC timestamp for display.
 *
 * - No opts, or opts.timezone === 'utc' (or undefined): UTC fast-path — drop
 *   sub-seconds and the 'T', append ' UTC'. NO Date parsing, NO zone shift.
 *   '2026-05-29T12:53:53.162712958Z' -> '2026-05-29 12:53:53 UTC'  (unchanged)
 *
 * - opts.timezone === 'local': convert the instant to the CONFIGURED zone
 *   (DEFAULT_DISPLAY_TZ, or opts.tz) and append ' <ZONE>':
 *   '2026-07-01T12:00:00.123Z' -> '2026-07-01 08:00:00 EDT'
 *
 * Returns `raw ?? ''` for null/undefined/empty. Returns the raw string
 * unchanged when it does not match the ISO-ish shape OR the instant is invalid.
 */
export function formatLogTimestamp(
  raw: string | null | undefined,
  opts?: LogTimestampOptions,
): string {
  if (raw == null || raw === '') return raw ?? ''

  // BACK-COMPAT: default + explicit 'utc' run the original regex fast-path,
  // byte-for-byte identical to the pre-STAGE-004-009 behavior.
  if (opts?.timezone !== 'local') {
    const match = LOG_TS_RE.exec(raw)
    if (match === null) return raw
    const date = match[1] ?? ''
    const time = match[2] ?? ''
    return `${date} ${time} UTC`
  }

  // 'local' = the CONFIGURED zone (DEFAULT_DISPLAY_TZ), via explicit Intl
  // timeZone — NOT the browser zone. Do not replace with toLocaleString().
  // Only convert inputs that match the same ISO-ish shape the UTC path accepts;
  // anything else falls through to the raw passthrough (matches UTC behavior).
  if (LOG_TS_RE.exec(raw) === null) return raw
  const instant = Date.parse(raw)
  if (Number.isNaN(instant)) return raw
  const tz = opts.tz ?? DEFAULT_DISPLAY_TZ
  const date = new Date(instant)

  // Wall-clock in the target zone. en-CA yields 'YYYY-MM-DD, HH:MM:SS' (24h).
  const wall = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
  // Normalize the comma separator -> single space: 'YYYY-MM-DD HH:MM:SS'.
  // en-CA + hour12:false can emit a "24:00:00" hour at midnight on some ICU
  // builds; normalize it to "00:" so midnight reads YYYY-MM-DD 00:00:00.
  const wallClock = wall.replace(', ', ' ').replace(/ 24:/, ' 00:')

  // Short zone label (EDT / EST) for the target zone at this instant.
  const zoneParts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    timeZoneName: 'short',
  }).formatToParts(date)
  const zone = zoneParts.find((p) => p.type === 'timeZoneName')?.value ?? ''

  return `${wallClock} ${zone}`
}

/**
 * Compose BOTH formats from the one shared formatter (NOT a parallel impl):
 * `display` is the requested format, `tooltip` is the OTHER format. Used to
 * set a native `title=` tooltip showing the alternate zone.
 */
export function formatLogTimestampParts(
  raw: string | null | undefined,
  opts?: LogTimestampOptions,
): { display: string; tooltip: string } {
  const activeTimezone: 'local' | 'utc' = opts?.timezone ?? 'local'
  const otherTimezone: 'local' | 'utc' = activeTimezone === 'utc' ? 'local' : 'utc'
  const display = formatLogTimestamp(raw, { ...opts, timezone: activeTimezone })
  const tooltip = formatLogTimestamp(raw, { ...opts, timezone: otherTimezone })
  return { display, tooltip }
}
