import { describe, it, expect } from 'vitest'

import { buildAlertRuleYaml } from '../alertRuleYaml'

describe('buildAlertRuleYaml', () => {
  it('renders a logsql rule with type: vlogs and the exact 042 structure', () => {
    const yaml = buildAlertRuleYaml({
      rule_name: 'HighErrorRate',
      expr: 'service:foo | stats count() as match_count | filter match_count:>10',
      expr_kind: 'logsql',
      severity: 'warning',
      summary: 'Too many errors',
      description: 'Errors exceeded threshold',
      for_duration: '5m',
    })
    expect(yaml).toBe(
      'groups:\n' +
        '  - name: user-rules-logs\n' +
        '    type: vlogs\n' +
        '    interval: 60s\n' +
        '    rules:\n' +
        '      - alert: HighErrorRate\n' +
        '        expr: |\n' +
        '          service:foo | stats count() as match_count | filter match_count:>10\n' +
        '        for: 5m\n' +
        '        labels:\n' +
        '          severity: warning\n' +
        '          source_tool: user\n' +
        '          category: user-rule\n' +
        '        annotations:\n' +
        '          summary: |\n' +
        '            Too many errors\n' +
        '          description: |\n' +
        '            Errors exceeded threshold\n',
    )
  })

  it('renders a metricsql rule WITHOUT type: vlogs and with the metrics group name', () => {
    const yaml = buildAlertRuleYaml({
      rule_name: 'CpuHigh',
      expr: 'rate(cpu[5m]) > 0.9',
      expr_kind: 'metricsql',
      severity: 'critical',
      summary: 'CPU high',
      description: '',
      for_duration: '0s',
    })
    expect(yaml).toContain('  - name: user-rules-metrics\n')
    expect(yaml).not.toContain('type: vlogs')
    expect(yaml).toContain('    interval: 60s\n')
    expect(yaml).toContain('      - alert: CpuHigh\n')
  })

  it('handles multi-line expr/summary with correct indentation', () => {
    const yaml = buildAlertRuleYaml({
      rule_name: 'MultiLine',
      expr: 'line1\nline2',
      expr_kind: 'logsql',
      severity: 'info',
      summary: 'sum1\nsum2',
      description: '',
      for_duration: '0s',
    })
    expect(yaml).toContain('          line1\n          line2\n')
    expect(yaml).toContain('            sum1\n            sum2\n')
  })
})
