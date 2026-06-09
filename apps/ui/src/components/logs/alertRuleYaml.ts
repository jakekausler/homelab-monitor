// STAGE-004-043 — client-side preview YAML, mirroring kernel/logs/user_rules_render.py
// render_yaml for a SINGLE rule. PREVIEW ONLY — the authoritative rule is rendered
// server-side. Kept pure + centralized so alertRuleYaml.test.ts pins the fidelity.

export interface AlertRuleYamlInput {
  rule_name: string
  expr: string
  expr_kind: 'logsql' | 'metricsql'
  severity: string
  summary: string
  description: string
  for_duration: string
}

const GROUP_INTERVAL = '60s'

/** Mirror of Python `_indent_block`: indent every line; empty lines collapse to ''. */
function indentBlock(value: string, indent: string): string {
  const lines = value.replace(/\n+$/, '').split('\n')
  return lines.map((ln) => (ln.length > 0 ? `${indent}${ln}` : '')).join('\n')
}

export function buildAlertRuleYaml(input: AlertRuleYamlInput): string {
  const groupName = input.expr_kind === 'logsql' ? 'user-rules-logs' : 'user-rules-metrics'
  const typeLine = input.expr_kind === 'logsql' ? '    type: vlogs\n' : ''

  const exprBody = indentBlock(input.expr, ' '.repeat(10))
  const summaryBody = indentBlock(input.summary, ' '.repeat(12))
  const descriptionBody = indentBlock(input.description, ' '.repeat(12))

  const ruleBlock =
    `      - alert: ${input.rule_name}\n` +
    `        expr: |\n` +
    `${exprBody}\n` +
    `        for: ${input.for_duration}\n` +
    `        labels:\n` +
    `          severity: ${input.severity}\n` +
    `          source_tool: user\n` +
    `          category: user-rule\n` +
    `        annotations:\n` +
    `          summary: |\n` +
    `${summaryBody}\n` +
    `          description: |\n` +
    `${descriptionBody}\n`

  return (
    `groups:\n  - name: ${groupName}\n${typeLine}` +
    `    interval: ${GROUP_INTERVAL}\n    rules:\n${ruleBlock}`
  )
}
