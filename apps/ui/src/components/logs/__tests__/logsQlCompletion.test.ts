import { describe, expect, it } from 'vitest'

import { logsQlCompletionSource } from '@/components/logs/logsQlCompletion'
import type { FieldsForCompletion } from '@/components/logs/logsQlCompletion'
import type { CompletionContext } from '@codemirror/autocomplete'

/**
 * Build a minimal CompletionContext over a single-line doc where the cursor sits
 * at the END of `text`. matchBefore emulates CM6: run the (anchored-at-$) regex
 * against the text before the cursor; on hit, return { from, to, text }.
 */
function ctx(text: string, explicit = false): CompletionContext {
  const pos = text.length
  const matchBefore = (re: RegExp): { from: number; to: number; text: string } | null => {
    const m = re.exec(text)
    if (m === null) return null
    const matched = m[0]
    const from = pos - matched.length
    return { from, to: pos, text: matched }
  }
  // Only the surface the pure source reads. Cast through unknown — the source
  // never touches state/view, so the partial mock is sufficient.
  return { pos, explicit, matchBefore } as unknown as CompletionContext
}

const FIELDS: FieldsForCompletion = [
  {
    name: 'service',
    coverage: 1,
    type_hint: 'string',
    sample_values: ['nginx', 'nginx-proxy', 'grafana'],
  },
  {
    name: 'severity',
    coverage: 0.9,
    type_hint: 'string',
    sample_values: ['error', 'warn', 'info'],
  },
  { name: 'json.user_id', coverage: 0.2, type_hint: 'numeric', sample_values: ['42', '7'] },
]

describe('logsQlCompletionSource', () => {
  describe('value context (field:partial)', () => {
    it("offers a field's sample_values filtered by the partial value", () => {
      const result = logsQlCompletionSource(ctx('service:ngi'), FIELDS)
      expect(result).not.toBeNull()
      expect(result?.options.map((o) => o.label)).toEqual(['nginx', 'nginx-proxy'])
    })

    it('anchors `from` at the start of the value (after the colon)', () => {
      const result = logsQlCompletionSource(ctx('service:ngi'), FIELDS)
      // 'service:' is 8 chars, value starts at index 8.
      expect(result?.from).toBe(8)
    })

    it('offers ALL sample_values when the value partial is empty (service:)', () => {
      const result = logsQlCompletionSource(ctx('service:'), FIELDS)
      expect(result?.options.map((o) => o.label)).toEqual(['nginx', 'nginx-proxy', 'grafana'])
    })

    it('returns null when the field is unknown', () => {
      const result = logsQlCompletionSource(ctx('nope:val'), FIELDS)
      expect(result).toBeNull()
    })

    it('returns null in value context when fields is undefined', () => {
      const result = logsQlCompletionSource(ctx('service:ngi'), undefined)
      expect(result).toBeNull()
    })
  })

  describe('keyword/pipe context (| partial)', () => {
    it('offers KEYWORDS filtered by the partial after a pipe', () => {
      const result = logsQlCompletionSource(ctx('* | st'), FIELDS)
      expect(result?.options.map((o) => o.label)).toContain('stats')
      expect(result?.options.every((o) => o.label.startsWith('st'))).toBe(true)
    })

    it('applies a trailing space to keyword completions', () => {
      const result = logsQlCompletionSource(ctx('* | sta'), FIELDS)
      const stats = result?.options.find((o) => o.label === 'stats')
      expect(stats?.apply).toBe('stats ')
    })

    it('returns null on empty pipe partial when not explicit', () => {
      const result = logsQlCompletionSource(ctx('* | '), FIELDS)
      expect(result).toBeNull()
    })

    it('offers all KEYWORDS on empty pipe partial when explicit', () => {
      const result = logsQlCompletionSource(ctx('* | ', true), FIELDS)
      expect(result?.options.length).toBeGreaterThan(0)
    })
  })

  describe('field-name context (bare word)', () => {
    it('offers the union of static FIELD_NAMES and discovered field names', () => {
      const result = logsQlCompletionSource(ctx('se'), FIELDS)
      const labels = result?.options.map((o) => o.label) ?? []
      expect(labels).toContain('service') // both static + discovered, deduped to one
      expect(labels).toContain('severity')
      // deduped: 'service' appears exactly once
      expect(labels.filter((l) => l === 'service')).toHaveLength(1)
    })

    it('appends a colon via apply for field-name completions', () => {
      const result = logsQlCompletionSource(ctx('host'), FIELDS)
      const host = result?.options.find((o) => o.label === 'host')
      expect(host?.apply).toBe('host:')
    })

    it('includes a discovered-only field name (json.user_id)', () => {
      const result = logsQlCompletionSource(ctx('json'), FIELDS)
      expect(result?.options.map((o) => o.label)).toContain('json.user_id')
    })

    it('returns null on empty bare partial when not explicit', () => {
      const result = logsQlCompletionSource(ctx(''), FIELDS)
      expect(result).toBeNull()
    })
  })

  describe('graceful degradation (no discovered fields)', () => {
    it('still offers static field names when fields is undefined', () => {
      const result = logsQlCompletionSource(ctx('host'), undefined)
      expect(result?.options.map((o) => o.label)).toContain('host')
    })

    it('still offers static field names when fields is empty', () => {
      const result = logsQlCompletionSource(ctx('host'), [])
      expect(result?.options.map((o) => o.label)).toContain('host')
    })

    it('does not throw on any context with undefined fields', () => {
      expect(() => logsQlCompletionSource(ctx('service:x'), undefined)).not.toThrow()
      expect(() => logsQlCompletionSource(ctx('* | '), undefined)).not.toThrow()
      expect(() => logsQlCompletionSource(ctx('ser'), undefined)).not.toThrow()
    })
  })
})
