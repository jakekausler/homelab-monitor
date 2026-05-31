import { describe, expect, it } from 'vitest'

import {
  ALL_PRESETS,
  fromDatetimeLocalValue,
  parseIso,
  presetToMs,
  resolveCustomWindow,
  resolvePreset,
  toDatetimeLocalValue,
  toIsoZ,
  validatePartialRange,
  validateRange,
} from '../timeRange'

const NOW = new Date('2026-05-30T12:00:00Z')

describe('presetToMs', () => {
  it('maps each preset to ms', () => {
    expect(presetToMs('5m')).toBe(5 * 60 * 1000)
    expect(presetToMs('1h')).toBe(60 * 60 * 1000)
    expect(presetToMs('7d')).toBe(7 * 24 * 60 * 60 * 1000)
  })
})

describe('resolvePreset', () => {
  it('returns a window ending at now', () => {
    const { start, end } = resolvePreset('1h', NOW)
    expect(end.getTime()).toBe(NOW.getTime())
    expect(end.getTime() - start.getTime()).toBe(60 * 60 * 1000)
  })
  it('defaults now to current time when omitted', () => {
    const { start, end } = resolvePreset('5m')
    expect(end.getTime() - start.getTime()).toBe(5 * 60 * 1000)
  })
})

describe('ALL_PRESETS', () => {
  it('has the six locked tokens', () => {
    expect(ALL_PRESETS).toEqual(['5m', '15m', '1h', '6h', '24h', '7d'])
  })
})

describe('validateRange', () => {
  const past = (h: number): Date => new Date(NOW.getTime() - h * 60 * 60 * 1000)

  it('accepts a valid past window', () => {
    expect(validateRange(past(2), past(1), { now: NOW })).toEqual({ ok: true })
  })

  it('rejects NaN dates', () => {
    const r = validateRange(new Date('bad'), past(1), { now: NOW })
    expect(r.ok).toBe(false)
  })

  it('rejects start >= end', () => {
    const r = validateRange(past(1), past(2), { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Start must be before end.' })
  })

  it('rejects equal start/end', () => {
    const r = validateRange(past(1), past(1), { now: NOW })
    expect(r.ok).toBe(false)
  })

  it('rejects a future start', () => {
    const future = new Date(NOW.getTime() + 60_000)
    const r = validateRange(future, new Date(NOW.getTime() + 120_000), { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Times cannot be in the future.' })
  })

  it('rejects a future end', () => {
    const r = validateRange(past(1), new Date(NOW.getTime() + 60_000), { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Times cannot be in the future.' })
  })

  it('rejects span over default 30 days', () => {
    const start = new Date(NOW.getTime() - 31 * 24 * 60 * 60 * 1000)
    const r = validateRange(start, NOW, { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Range cannot exceed 30 days.' })
  })

  it('accepts span exactly at the cap', () => {
    const start = new Date(NOW.getTime() - 30 * 24 * 60 * 60 * 1000)
    expect(validateRange(start, NOW, { now: NOW })).toEqual({ ok: true })
  })

  it('honors a custom maxSpanDays', () => {
    const start = new Date(NOW.getTime() - 2 * 24 * 60 * 60 * 1000)
    const r = validateRange(start, NOW, { now: NOW, maxSpanDays: 1 })
    expect(r).toEqual({ ok: false, error: 'Range cannot exceed 1 days.' })
  })

  it('bounded mode rejects start before min', () => {
    const min = past(3)
    const r = validateRange(past(4), past(1), { now: NOW, min })
    expect(r).toEqual({ ok: false, error: 'Start is before the available window.' })
  })

  it('bounded mode rejects end after max', () => {
    const max = past(2)
    const r = validateRange(past(4), past(1), { now: NOW, max })
    expect(r).toEqual({ ok: false, error: 'End is after the available window.' })
  })

  it('bounded mode accepts a window inside [min,max]', () => {
    const min = past(5)
    const max = past(1)
    expect(validateRange(past(4), past(2), { now: NOW, min, max })).toEqual({ ok: true })
  })

  it('uses real now when opts.now omitted (future rejected)', () => {
    const future = new Date(Date.now() + 60 * 60 * 1000)
    const r = validateRange(new Date(Date.now() - 60 * 60 * 1000), future)
    expect(r.ok).toBe(false)
  })
})

describe('toIsoZ / parseIso', () => {
  it('round-trips a date', () => {
    const d = new Date('2026-05-01T00:00:00.000Z')
    expect(toIsoZ(d)).toBe('2026-05-01T00:00:00.000Z')
    expect(parseIso(toIsoZ(d))?.getTime()).toBe(d.getTime())
  })
  it('parseIso returns null on garbage', () => {
    expect(parseIso('not-a-date')).toBeNull()
  })
})

describe('toDatetimeLocalValue / fromDatetimeLocalValue', () => {
  it('round-trips through local value form', () => {
    const d = new Date(2026, 4, 1, 9, 30) // local-time constructor
    const v = toDatetimeLocalValue(d)
    expect(v).toBe('2026-05-01T09:30')
    expect(fromDatetimeLocalValue(v)?.getTime()).toBe(d.getTime())
  })
  it('fromDatetimeLocalValue returns null on empty', () => {
    expect(fromDatetimeLocalValue('')).toBeNull()
  })
  it('fromDatetimeLocalValue returns null on invalid', () => {
    expect(fromDatetimeLocalValue('garbage')).toBeNull()
  })
})

describe('resolveCustomWindow', () => {
  const NOW = new Date('2026-05-30T12:00:00Z')
  const DAY = 24 * 60 * 60 * 1000
  const SPAN30 = 30 * DAY

  it('open end resolves to now', () => {
    const { end } = resolveCustomWindow({ start: new Date(NOW.getTime() - DAY) }, { now: NOW })
    expect(end.getTime()).toBe(NOW.getTime())
  })

  it('open start resolves to now − 30d', () => {
    const { start } = resolveCustomWindow({ end: NOW }, { now: NOW })
    expect(start.getTime()).toBe(NOW.getTime() - SPAN30)
  })

  it('both open resolves to [now−30d, now]', () => {
    const { start, end } = resolveCustomWindow({}, { now: NOW })
    expect(end.getTime()).toBe(NOW.getTime())
    expect(start.getTime()).toBe(NOW.getTime() - SPAN30)
  })

  it('bounded: open end clamps to max', () => {
    const max = new Date(NOW.getTime() - DAY)
    const { end } = resolveCustomWindow({}, { now: NOW, max })
    expect(end.getTime()).toBe(max.getTime())
  })

  it('bounded: open start clamps to min when run window is shorter than 30d', () => {
    const min = new Date(NOW.getTime() - 2 * DAY)
    const max = new Date(NOW.getTime() - DAY)
    const { start } = resolveCustomWindow({}, { now: NOW, min, max })
    expect(start.getTime()).toBe(min.getTime())
  })

  it('start clamp: when (end − 30d) < min, resolves to min', () => {
    const min = new Date(NOW.getTime() - DAY)
    const end = new Date(NOW.getTime()) // end − 30d is way before min
    const { start } = resolveCustomWindow({ end }, { now: NOW, min })
    expect(start.getTime()).toBe(min.getTime())
  })
})

describe('validatePartialRange', () => {
  const NOW = new Date('2026-05-30T12:00:00Z')
  const past = (h: number) => new Date(NOW.getTime() - h * 60 * 60 * 1000)

  it('only-start in the future → reject', () => {
    const future = new Date(NOW.getTime() + 60_000)
    const r = validatePartialRange(future, null, { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Times cannot be in the future.' })
  })

  it('only-end in the future → reject', () => {
    const future = new Date(NOW.getTime() + 60_000)
    const r = validatePartialRange(null, future, { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Times cannot be in the future.' })
  })

  it('only-start valid (end open) → ok', () => {
    const r = validatePartialRange(past(2), null, { now: NOW })
    expect(r).toEqual({ ok: true })
  })

  it('only-end valid (start open) → ok', () => {
    const r = validatePartialRange(null, past(1), { now: NOW })
    expect(r).toEqual({ ok: true })
  })

  it('both null → ok', () => {
    const r = validatePartialRange(null, null, { now: NOW })
    expect(r).toEqual({ ok: true })
  })

  it('both provided, start >= end → reject (delegates to validateRange)', () => {
    const r = validatePartialRange(past(1), past(2), { now: NOW })
    expect(r).toEqual({ ok: false, error: 'Start must be before end.' })
  })

  it('both provided, valid → ok', () => {
    const r = validatePartialRange(past(2), past(1), { now: NOW })
    expect(r).toEqual({ ok: true })
  })
})
