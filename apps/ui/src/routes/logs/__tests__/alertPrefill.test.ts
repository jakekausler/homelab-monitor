import { describe, it, expect } from 'vitest'

// Pure imports — no mocks needed (alertExpr.ts has zero deps; logsQlTranslate is pure)
import {
  deriveRuleName,
  prefillFromSignature,
  prefillFromSavedQuery,
  composeServicesExpr,
} from '../alertPrefill'
import type { SignatureResponse } from '@/api/signatures'
import type { SavedQuery } from '@/api/savedLogQueries'

// ---------------------------------------------------------------------------
// deriveRuleName
// ---------------------------------------------------------------------------
describe('deriveRuleName', () => {
  it('produces a valid identifier for clean inputs', () => {
    const result = deriveRuleName('SignatureSpike', 'abc123', 'myservice')
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('replaces colons with underscores (service_key contains colon)', () => {
    const result = deriveRuleName('SignatureSpike', 'hash1', 'docker:nginx')
    expect(result).not.toContain(':')
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('replaces hyphens with underscores', () => {
    const result = deriveRuleName('SavedQuery', '42', 'my-query-name')
    expect(result).not.toContain('-')
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('replaces spaces with underscores', () => {
    const result = deriveRuleName('SavedQuery', '1', 'my query')
    expect(result).not.toContain(' ')
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('replaces dots with underscores', () => {
    const result = deriveRuleName('SignatureSpike', 'abc', 'service.name')
    expect(result).not.toContain('.')
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('prepends r_ when first char would be a digit', () => {
    // prefix starts with digit — highly unlikely in practice but must be guarded
    const result = deriveRuleName('123bad', 'id', 'name')
    expect(result).toMatch(/^r_/)
    expect(result).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('is deterministic — same inputs always produce same output', () => {
    const a = deriveRuleName('SignatureSpike', 'hash1', 'docker:nginx')
    const b = deriveRuleName('SignatureSpike', 'hash1', 'docker:nginx')
    expect(a).toBe(b)
  })

  it('truncates to 200 chars', () => {
    const longName = 'a'.repeat(300)
    const result = deriveRuleName('SignatureSpike', 'hash', longName)
    expect(result.length).toBeLessThanOrEqual(200)
  })
})

// ---------------------------------------------------------------------------
// prefillFromSignature
// ---------------------------------------------------------------------------

const makeSig = (overrides?: Partial<SignatureResponse>): SignatureResponse => ({
  template_hash: 'abc123def456',
  service_key: 'docker:nginx',
  template_str: 'Connection * from * port *',
  label: null,
  status: 'active',
  first_seen_at: 1000000,
  last_seen_at: 2000000,
  total_count: 42,
  ...overrides,
})

describe('prefillFromSignature', () => {
  it('returns advanced mode', () => {
    const result = prefillFromSignature(makeSig())
    expect(result.initialMode).toBe('advanced')
  })

  it('sets expr_kind to logsql', () => {
    const result = prefillFromSignature(makeSig())
    expect(result.initialValues.expr_kind).toBe('logsql')
  })

  it('scaffolds expr with | stats count() as match_count', () => {
    const result = prefillFromSignature(makeSig())
    expect(result.initialValues.expr).toContain('| stats count() as match_count')
  })

  it('does not double-scaffold if template already contains | stats', () => {
    // templateToLogsQl would return the longest literal run; this tests scaffold idempotency
    const sig = makeSig({
      template_str: 'foo | stats count() as match_count | filter match_count:>10',
    })
    const result = prefillFromSignature(sig)
    // scaffoldLogsqlExpr should not append a second stats pipe
    const statsMatches = (result.initialValues.expr ?? '').match(/\|\s*stats/gi)
    expect(statsMatches?.length).toBe(1)
  })

  it('sets severity to warning', () => {
    expect(prefillFromSignature(makeSig()).initialValues.severity).toBe('warning')
  })

  it('sets for_duration to 1m', () => {
    expect(prefillFromSignature(makeSig()).initialValues.for_duration).toBe('1m')
  })

  it('sets sourceKind to signature', () => {
    expect(prefillFromSignature(makeSig()).sourceKind).toBe('signature')
  })

  it('sets sourceRef as template_hash:service_key', () => {
    const sig = makeSig({ template_hash: 'abc123', service_key: 'docker:nginx' })
    expect(prefillFromSignature(sig).sourceRef).toBe('abc123:docker:nginx')
  })

  it('sets rule_name matching RULE_NAME_REGEX', () => {
    const result = prefillFromSignature(makeSig())
    expect(result.initialValues.rule_name).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })

  it('includes template_hash in summary', () => {
    const sig = makeSig({ template_hash: 'abc123' })
    const result = prefillFromSignature(sig)
    expect(result.initialValues.summary).toBeDefined()
    expect(result.initialValues.summary).toContain('abc123')
  })
})

// ---------------------------------------------------------------------------
// prefillFromSavedQuery
// ---------------------------------------------------------------------------

const makeSavedQuery = (overrides?: Partial<SavedQuery>): SavedQuery => ({
  id: 42,
  name: 'My Query',
  logs_ql: 'service:nginx "error"',
  advanced_mode: false,
  selected_services: [],
  since_preset: '1h',
  range_start_iso: null,
  range_end_iso: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
})

describe('prefillFromSavedQuery — simple mode (advanced_mode=false)', () => {
  it('returns simple mode', () => {
    const result = prefillFromSavedQuery(makeSavedQuery({ advanced_mode: false }))
    expect(result.initialMode).toBe('simple')
  })

  it('sets simple_contains to logs_ql', () => {
    const sq = makeSavedQuery({ advanced_mode: false, logs_ql: 'service:nginx "error"' })
    const result = prefillFromSavedQuery(sq)
    expect(result.initialValues.simple_contains).toBe('service:nginx "error"')
  })

  it('does NOT set expr in simple mode', () => {
    const result = prefillFromSavedQuery(makeSavedQuery({ advanced_mode: false }))
    expect(result.initialValues.expr).toBeUndefined()
  })

  it('sets expr_kind to logsql', () => {
    const result = prefillFromSavedQuery(makeSavedQuery({ advanced_mode: false }))
    expect(result.initialValues.expr_kind).toBe('logsql')
  })

  it('sets severity to warning', () => {
    expect(prefillFromSavedQuery(makeSavedQuery()).initialValues.severity).toBe('warning')
  })

  it('sets for_duration to 5m', () => {
    expect(prefillFromSavedQuery(makeSavedQuery()).initialValues.for_duration).toBe('5m')
  })

  it('sets sourceKind to saved_query', () => {
    expect(prefillFromSavedQuery(makeSavedQuery()).sourceKind).toBe('saved_query')
  })

  it('sets sourceRef to string of id', () => {
    expect(prefillFromSavedQuery(makeSavedQuery({ id: 99 })).sourceRef).toBe('99')
  })

  it('sets rule_name matching RULE_NAME_REGEX', () => {
    const result = prefillFromSavedQuery(makeSavedQuery())
    expect(result.initialValues.rule_name).toMatch(/^[a-zA-Z_][a-zA-Z0-9_]*$/)
  })
})

describe('prefillFromSavedQuery — advanced mode (advanced_mode=true)', () => {
  it('returns advanced mode', () => {
    const result = prefillFromSavedQuery(makeSavedQuery({ advanced_mode: true }))
    expect(result.initialMode).toBe('advanced')
  })

  it('scaffolds expr with | stats count() as match_count', () => {
    const sq = makeSavedQuery({ advanced_mode: true, logs_ql: 'service:nginx' })
    const result = prefillFromSavedQuery(sq)
    expect(result.initialValues.expr).toContain('| stats count() as match_count')
  })

  it('does NOT set simple_contains in advanced mode', () => {
    const result = prefillFromSavedQuery(makeSavedQuery({ advanced_mode: true }))
    expect(result.initialValues.simple_contains).toBeUndefined()
  })

  it('passes through already-scaffolded expr unchanged', () => {
    const scaffolded = 'service:foo | stats count() as match_count | filter match_count:>10'
    const sq = makeSavedQuery({ advanced_mode: true, logs_ql: scaffolded })
    const result = prefillFromSavedQuery(sq)
    expect(result.initialValues.expr).toBe(scaffolded)
  })
})

// ---------------------------------------------------------------------------
// composeServicesExpr
// ---------------------------------------------------------------------------
describe('composeServicesExpr', () => {
  it('single service — wraps clause and base in parens, joined with AND', () => {
    const result = composeServicesExpr('error', [{ service: 'nginx', source_type: 'docker' }])
    expect(result).toBe('(service:"nginx" AND source_type:"docker") AND (error)')
  })

  it('multiple services — OR-groups clauses, then ANDs with base', () => {
    const result = composeServicesExpr('warn', [
      { service: 'a', source_type: 'docker' },
      { service: 'b', source_type: 'docker' },
    ])
    expect(result).toBe(
      '((service:"a" AND source_type:"docker") OR (service:"b" AND source_type:"docker")) AND (warn)',
    )
  })

  it('empty services array — returns baseExpr unchanged', () => {
    expect(composeServicesExpr('my query', [])).toBe('my query')
  })

  it('empty baseExpr + single service — returns just the service clause (no trailing AND ())', () => {
    const result = composeServicesExpr('', [{ service: 'nginx', source_type: 'docker' }])
    expect(result).toBe('(service:"nginx" AND source_type:"docker")')
    expect(result).not.toContain('AND ()')
  })

  it('empty baseExpr + multiple services — returns just the OR-group (no trailing AND ())', () => {
    const result = composeServicesExpr('', [
      { service: 'grafana', source_type: 'docker' },
      { service: 'victorialogs', source_type: 'docker' },
    ])
    expect(result).toBe(
      '((service:"grafana" AND source_type:"docker") OR (service:"victorialogs" AND source_type:"docker"))',
    )
    expect(result).not.toContain('AND ()')
  })

  it('malformed entry (empty service) is skipped', () => {
    const result = composeServicesExpr('q', [
      { service: '', source_type: 'docker' },
      { service: 'nginx', source_type: 'docker' },
    ])
    expect(result).toBe('(service:"nginx" AND source_type:"docker") AND (q)')
  })

  it('malformed entry (empty source_type) is skipped', () => {
    const result = composeServicesExpr('q', [
      { service: 'nginx', source_type: '' },
      { service: 'nginx', source_type: 'docker' },
    ])
    expect(result).toBe('(service:"nginx" AND source_type:"docker") AND (q)')
  })

  it('all entries malformed — returns baseExpr unchanged', () => {
    expect(composeServicesExpr('base', [{ service: '', source_type: '' }])).toBe('base')
  })

  it('quoting escapes a double-quote in the service name', () => {
    const result = composeServicesExpr('q', [{ service: 'say"hi"', source_type: 'docker' }])
    // escapeLogsQlPhrase converts " → \" inside double-quoted string
    expect(result).toBe('(service:"say\\"hi\\"" AND source_type:"docker") AND (q)')
  })

  it('uses service:"x" form (no = prefix) matching backend', () => {
    const result = composeServicesExpr('q', [{ service: 'nginx', source_type: 'docker' }])
    expect(result).not.toContain('service:=')
    expect(result).not.toContain('source_type:=')
    expect(result).toContain('service:"nginx"')
    expect(result).toContain('source_type:"docker"')
  })
})

// ---------------------------------------------------------------------------
// prefillFromSavedQuery — with selected_services
// ---------------------------------------------------------------------------
describe('prefillFromSavedQuery — service-scoped (selected_services non-empty)', () => {
  it('plain mode sq with services → ADVANCED mode (not simple)', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    expect(prefillFromSavedQuery(sq).initialMode).toBe('advanced')
  })

  it('advanced mode sq with services → ADVANCED mode', () => {
    const sq = makeSavedQuery({
      advanced_mode: true,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    expect(prefillFromSavedQuery(sq).initialMode).toBe('advanced')
  })

  it('expr contains the service clause AND the logs_ql base', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    const { expr } = prefillFromSavedQuery(sq).initialValues
    expect(expr).toContain('service:"nginx"')
    expect(expr).toContain('source_type:"docker"')
    expect(expr).toContain('error')
  })

  it('expr ends with | stats count() as match_count | filter match_count:>10', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    const { expr } = prefillFromSavedQuery(sq).initialValues
    expect(expr).toMatch(/\| stats count\(\) as match_count \| filter match_count:>10$/)
  })

  it('uses match_count alias (not reserved "count")', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: 'warn',
      selected_services: [{ service: 'alertmanager', source_type: 'docker' }],
    })
    const { expr } = prefillFromSavedQuery(sq).initialValues
    expect(expr).toContain('match_count')
    // Ensure it's not using a bare `count` alias
    expect(expr).not.toMatch(/as count[^_]/)
  })

  it('multi-service sq with logs_ql="" — no AND () in expr, has stats pipe', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: '',
      selected_services: [
        { service: 'grafana', source_type: 'docker' },
        { service: 'victorialogs', source_type: 'docker' },
      ],
    })
    const { expr } = prefillFromSavedQuery(sq).initialValues
    expect(expr).not.toContain('AND ()')
    expect(expr).toContain('service:"grafana"')
    expect(expr).toContain('service:"victorialogs"')
    expect(expr).toContain('| stats count() as match_count')
  })

  it('does NOT set simple_contains in service-scoped mode', () => {
    const sq = makeSavedQuery({
      advanced_mode: false,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    expect(prefillFromSavedQuery(sq).initialValues.simple_contains).toBeUndefined()
  })

  it('sourceKind remains saved_query, sourceRef is string id', () => {
    const sq = makeSavedQuery({
      id: 77,
      advanced_mode: false,
      logs_ql: 'error',
      selected_services: [{ service: 'nginx', source_type: 'docker' }],
    })
    const result = prefillFromSavedQuery(sq)
    expect(result.sourceKind).toBe('saved_query')
    expect(result.sourceRef).toBe('77')
  })
})

describe('prefillFromSavedQuery — no selected_services (regression: existing behavior unchanged)', () => {
  it('plain sq with no services → simple mode', () => {
    const sq = makeSavedQuery({ advanced_mode: false, selected_services: [] })
    expect(prefillFromSavedQuery(sq).initialMode).toBe('simple')
  })

  it('advanced sq with no services → advanced mode', () => {
    const sq = makeSavedQuery({
      advanced_mode: true,
      logs_ql: 'service:foo',
      selected_services: [],
    })
    expect(prefillFromSavedQuery(sq).initialMode).toBe('advanced')
  })

  it('plain sq sets simple_contains when no services', () => {
    const sq = makeSavedQuery({ advanced_mode: false, logs_ql: 'my query', selected_services: [] })
    expect(prefillFromSavedQuery(sq).initialValues.simple_contains).toBe('my query')
  })

  it('advanced sq scaffolds expr when no services', () => {
    const sq = makeSavedQuery({
      advanced_mode: true,
      logs_ql: 'service:nginx',
      selected_services: [],
    })
    const { expr } = prefillFromSavedQuery(sq).initialValues
    expect(expr).toContain('| stats count() as match_count')
  })
})
