import { describe, expect, it } from 'vitest'

import { classesFor, parseAnsi } from '@/components/logs/ansi'

const ESC = '\x1b['

describe('parseAnsi', () => {
  it('fast-path: plain text with no escapes returns one plain segment', () => {
    const segs = parseAnsi('hello world')
    expect(segs).toEqual([{ text: 'hello world', classes: '' }])
  })

  it('empty string returns one empty plain segment', () => {
    expect(parseAnsi('')).toEqual([{ text: '', classes: '' }])
  })

  it('single fg color then reset', () => {
    const segs = parseAnsi(`${ESC}31mred${ESC}0m plain`)
    expect(segs[0]).toEqual({ text: 'red', classes: 'text-red-500' })
    expect(segs[1]).toEqual({ text: ' plain', classes: '' })
  })

  it('bold + color combine into space-joined classes', () => {
    const segs = parseAnsi(`${ESC}1m${ESC}32mok${ESC}0m`)
    expect(segs[0]?.text).toBe('ok')
    expect(segs[0]?.classes).toContain('text-green-500')
    expect(segs[0]?.classes).toContain('font-bold')
  })

  it('background color emits a bg class', () => {
    const segs = parseAnsi(`${ESC}41mhi${ESC}0m`)
    expect(segs[0]?.classes).toBe('bg-red-500/30')
  })

  it('overlapping/nested styles track state across runs', () => {
    // bold on, then fg red, text, then bold off (22) keeps red
    const segs = parseAnsi(`${ESC}1mA${ESC}31mB${ESC}22mC${ESC}0m`)
    expect(segs[0]).toEqual({ text: 'A', classes: 'font-bold' })
    expect(segs[1]?.classes).toContain('font-bold')
    expect(segs[1]?.classes).toContain('text-red-500')
    expect(segs[1]?.text).toBe('B')
    // after 22, bold is off but red remains
    expect(segs[2]?.classes).toBe('text-red-500')
    expect(segs[2]?.text).toBe('C')
  })

  it('unterminated escape at end of line does not crash; trailing text renders', () => {
    const segs = parseAnsi(`done${ESC}`)
    const joined = segs.map((s) => s.text).join('')
    expect(joined).toContain('done')
  })

  it('empty params (ESC[m) is treated as a full reset', () => {
    const segs = parseAnsi(`${ESC}31mred${ESC}mplain`)
    expect(segs[0]).toEqual({ text: 'red', classes: 'text-red-500' })
    expect(segs[1]).toEqual({ text: 'plain', classes: '' })
  })

  it('unknown code is ignored', () => {
    const segs = parseAnsi(`${ESC}99mtext${ESC}0m`)
    expect(segs[0]).toEqual({ text: 'text', classes: '' })
  })

  it('256-color params are consumed, not leaked into text', () => {
    const segs = parseAnsi(`${ESC}38;5;200mx${ESC}0m`)
    const joined = segs.map((s) => s.text).join('')
    expect(joined).toBe('x')
    expect(joined).not.toContain('5')
    expect(joined).not.toContain('200')
  })

  it('truecolor params are consumed, not leaked into text', () => {
    const segs = parseAnsi(`${ESC}38;2;10;20;30my${ESC}0m`)
    const joined = segs.map((s) => s.text).join('')
    expect(joined).toBe('y')
    expect(joined).not.toContain('10')
  })

  it('underline + bg + dim combine', () => {
    const segs = parseAnsi(`${ESC}4m${ESC}2m${ESC}44mz${ESC}0m`)
    expect(segs[0]?.classes).toContain('underline')
    expect(segs[0]?.classes).toContain('opacity-70')
    expect(segs[0]?.classes).toContain('bg-blue-500/30')
  })
})

describe('classesFor', () => {
  it('returns empty string for empty state', () => {
    expect(classesFor({ fg: '', bg: '', bold: false, dim: false, underline: false })).toBe('')
  })

  it('joins active classes in fg/bg/bold/dim/underline order', () => {
    expect(
      classesFor({
        fg: 'text-red-500',
        bg: 'bg-blue-500/30',
        bold: true,
        dim: true,
        underline: true,
      }),
    ).toBe('text-red-500 bg-blue-500/30 font-bold opacity-70 underline')
  })
})
