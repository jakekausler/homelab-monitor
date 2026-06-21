import { describe, expect, it } from 'vitest'

import {
  UDM_LOG_CATEGORIES,
  buildUdmLogsExpr,
  buildUdmThreatsExpr,
  type UdmLogCategory,
} from '../udmLogFilters'

describe('buildUdmLogsExpr', () => {
  it('all category uses the udm-* wildcard, no IP', () => {
    expect(buildUdmLogsExpr('all')).toBe('source_type:udm service:udm-*')
  })

  it('firewall category', () => {
    expect(buildUdmLogsExpr('firewall')).toBe('source_type:udm service:udm-firewall')
  })

  it('audit category', () => {
    expect(buildUdmLogsExpr('audit')).toBe('source_type:udm service:udm-audit')
  })

  it('system category', () => {
    expect(buildUdmLogsExpr('system')).toBe('source_type:udm service:udm-system')
  })

  it('appends a src/dst OR group when an IP is given', () => {
    expect(buildUdmLogsExpr('all', '10.0.0.5')).toBe(
      'source_type:udm service:udm-* (src:"10.0.0.5" OR dst:"10.0.0.5")',
    )
  })

  it('combines category + IP', () => {
    expect(buildUdmLogsExpr('firewall', '192.168.2.1')).toBe(
      'source_type:udm service:udm-firewall (src:"192.168.2.1" OR dst:"192.168.2.1")',
    )
  })

  it('ignores blank / whitespace-only IP (no OR group)', () => {
    expect(buildUdmLogsExpr('all', '   ')).toBe('source_type:udm service:udm-*')
    expect(buildUdmLogsExpr('all', '')).toBe('source_type:udm service:udm-*')
  })

  it('escapes a double-quote in the IP value via fieldFilterClause', () => {
    // Defensive: even though IPs do not contain quotes, the escaping path must be exercised.
    expect(buildUdmLogsExpr('all', 'a"b')).toBe(
      'source_type:udm service:udm-* (src:"a\\"b" OR dst:"a\\"b")',
    )
  })

  it('trims surrounding whitespace from the IP', () => {
    expect(buildUdmLogsExpr('all', '  10.0.0.5  ')).toBe(
      'source_type:udm service:udm-* (src:"10.0.0.5" OR dst:"10.0.0.5")',
    )
  })
})

describe('buildUdmThreatsExpr', () => {
  it('pins to audit and firewall services, no IP', () => {
    expect(buildUdmThreatsExpr()).toBe(
      'source_type:udm (service:udm-audit OR service:udm-firewall)',
    )
  })

  it('appends src/dst OR group when an IP is given', () => {
    expect(buildUdmThreatsExpr('10.0.0.5')).toBe(
      'source_type:udm (service:udm-audit OR service:udm-firewall) (src:"10.0.0.5" OR dst:"10.0.0.5")',
    )
  })
})

describe('UDM_LOG_CATEGORIES', () => {
  it('covers exactly the four categories with labels', () => {
    const values = UDM_LOG_CATEGORIES.map((c) => c.value)
    expect(values).toEqual<UdmLogCategory[]>(['all', 'firewall', 'audit', 'system'])
    expect(UDM_LOG_CATEGORIES.map((c) => c.label)).toEqual(['All', 'Firewall', 'Audit', 'System'])
  })
})
