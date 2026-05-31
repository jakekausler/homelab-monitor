// STAGE-004-008 — shared time-range types + pure validation/format helpers.
// 100%-tested pure logic (see __tests__/timeRange.test.ts). Consumed by
// <TimeRangeControl>, the Docker log viewer, the Cron log viewer, and
// SCAFFOLDING: STAGE-004-010 Explorer.

export type PresetToken = '5m' | '15m' | '1h' | '6h' | '24h' | '7d'

export const ALL_PRESETS: readonly PresetToken[] = ['5m', '15m', '1h', '6h', '24h', '7d']

export type TimeRangeValue =
  | { kind: 'preset'; token: PresetToken }
  | { kind: 'custom'; start?: Date | undefined; end?: Date | undefined }

const PRESET_HOURS: Record<PresetToken, number> = {
  '5m': 5 / 60,
  '15m': 15 / 60,
  '1h': 1,
  '6h': 6,
  '24h': 24,
  '7d': 24 * 7,
}

/** Resolve a preset token to its window length in milliseconds. */
export function presetToMs(token: PresetToken): number {
  return PRESET_HOURS[token] * 60 * 60 * 1000
}

/**
 * Resolve a preset token to an absolute [start, end] window ending at `now`.
 * Used by viewers that need ISO start/end for the backend (Explorer; and
 * potentially the Docker viewer if it ever sends absolute presets — currently
 * it sends the token via `since`).
 */
export function resolvePreset(
  token: PresetToken,
  now: Date = new Date(),
): {
  start: Date
  end: Date
} {
  return { start: new Date(now.getTime() - presetToMs(token)), end: now }
}

export interface ValidateRangeOpts {
  min?: Date | undefined
  max?: Date | undefined
  maxSpanDays?: number | undefined
  now?: Date | undefined
}

export type ValidateResult = { ok: true } | { ok: false; error: string }

const DEFAULT_MAX_SPAN_DAYS = 30

/**
 * Validate a custom [start, end] range.
 * Rules:
 *  - both must be valid Dates.
 *  - start strictly before end.
 *  - neither in the future (vs opts.now ?? new Date()).
 *  - span <= maxSpanDays (default 30).
 *  - bounded mode: start >= min and end <= max (when provided).
 */
export function validateRange(
  start: Date,
  end: Date,
  opts: ValidateRangeOpts = {},
): ValidateResult {
  const now = opts.now ?? new Date()
  const maxSpanDays = opts.maxSpanDays ?? DEFAULT_MAX_SPAN_DAYS

  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return { ok: false, error: 'Enter a valid start and end time.' }
  }
  if (start.getTime() >= end.getTime()) {
    return { ok: false, error: 'Start must be before end.' }
  }
  if (start.getTime() > now.getTime() || end.getTime() > now.getTime()) {
    return { ok: false, error: 'Times cannot be in the future.' }
  }
  const spanMs = end.getTime() - start.getTime()
  if (spanMs > maxSpanDays * 24 * 60 * 60 * 1000) {
    return { ok: false, error: `Range cannot exceed ${String(maxSpanDays)} days.` }
  }
  if (opts.min !== undefined && start.getTime() < opts.min.getTime()) {
    return { ok: false, error: 'Start is before the available window.' }
  }
  if (opts.max !== undefined && end.getTime() > opts.max.getTime()) {
    return { ok: false, error: 'End is after the available window.' }
  }
  return { ok: true }
}

export interface ResolveCustomWindowOpts {
  /** Lower clamp for an open start (e.g. cron run-window start). */
  min?: Date | undefined
  /** Upper clamp for an open end (e.g. cron run-window end). */
  max?: Date | undefined
  /** Injected "now" — REQUIRED so the resolver is deterministic in tests. */
  now: Date
  /** Span used to default an open start back from the resolved end. Default 30. */
  maxSpanDays?: number | undefined
}

/**
 * Resolve a custom range with optional open bounds to a concrete [start, end].
 *
 * Rules:
 *   end   = value.end   ?? opts.max ?? opts.now
 *   start = value.start ?? max(resolvedEnd − maxSpanDays days, opts.min ?? -Infinity)
 *
 * On docker (no min/max): open end → now; open start → end − 30d.
 * On cron (min=runStart, max=runEnd): open end → runEnd; open start → max(runStart, end−30d)
 *   which equals runStart for short run windows.
 *
 * `now` is INJECTED (never call new Date() here) for deterministic tests.
 */
export function resolveCustomWindow(
  value: { start?: Date | undefined; end?: Date | undefined },
  opts: ResolveCustomWindowOpts,
): { start: Date; end: Date } {
  const maxSpanDays = opts.maxSpanDays ?? DEFAULT_MAX_SPAN_DAYS
  const end = value.end ?? opts.max ?? opts.now
  let start: Date
  if (value.start !== undefined) {
    start = value.start
  } else {
    const spanFloor = new Date(end.getTime() - maxSpanDays * 24 * 60 * 60 * 1000)
    start =
      opts.min !== undefined && opts.min.getTime() > spanFloor.getTime() ? opts.min : spanFloor
  }
  return { start, end }
}

/**
 * Validate a custom range where EITHER bound may be empty (open bound).
 * Used by the picker, which now accepts open start/end.
 *
 * Rules:
 *   - any PROVIDED bound must be a valid Date.
 *   - any PROVIDED bound must not be in the future (vs opts.now ?? new Date()).
 *   - if BOTH provided: start < end, span <= maxSpanDays, and bounded min/max.
 *   - if a bound is empty: skip the rules that need it; the open bound is
 *     later CLAMPED (not rejected) by resolveCustomWindow, and the backend re-validates the resolved window.
 *   - both empty: ok.
 *
 * `start`/`end` are `Date | null` (null = empty, matching
 * fromDatetimeLocalValue's return). When BOTH are non-null this delegates to
 * validateRange so error strings stay identical.
 */
export function validatePartialRange(
  start: Date | null,
  end: Date | null,
  opts: ValidateRangeOpts = {},
): ValidateResult {
  const now = opts.now ?? new Date()

  if (start !== null && Number.isNaN(start.getTime())) {
    return { ok: false, error: 'Enter a valid start and end time.' }
  }
  if (end !== null && Number.isNaN(end.getTime())) {
    return { ok: false, error: 'Enter a valid start and end time.' }
  }
  if (start !== null && start.getTime() > now.getTime()) {
    return { ok: false, error: 'Times cannot be in the future.' }
  }
  if (end !== null && end.getTime() > now.getTime()) {
    return { ok: false, error: 'Times cannot be in the future.' }
  }
  if (start !== null && end !== null) {
    // Both provided → full validation (order, span, bounds) via validateRange.
    return validateRange(start, end, opts)
  }
  // One or zero provided → provided-only checks already passed.
  return { ok: true }
}

/** Date → ISO-8601 UTC string (e.g. "2026-05-01T00:00:00.000Z"). */
export function toIsoZ(date: Date): string {
  return date.toISOString()
}

/** Parse an ISO string to a Date. Returns null when unparseable. */
export function parseIso(raw: string): Date | null {
  const d = new Date(raw)
  return Number.isNaN(d.getTime()) ? null : d
}

/**
 * Convert a Date to the value string a native <input type="datetime-local">
 * expects in LOCAL time: "YYYY-MM-DDTHH:mm". (datetime-local has no timezone;
 * the browser interprets it as local. We convert back to a UTC Date on read.)
 */
export function toDatetimeLocalValue(date: Date): string {
  const pad = (n: number): string => String(n).padStart(2, '0')
  return (
    `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/**
 * Parse a native datetime-local value ("YYYY-MM-DDTHH:mm", local time) into a
 * Date. Returns null on empty/invalid. `new Date(localString)` interprets a
 * datetime-local string as LOCAL time, which is what we want.
 */
export function fromDatetimeLocalValue(value: string): Date | null {
  if (value.length === 0) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}
