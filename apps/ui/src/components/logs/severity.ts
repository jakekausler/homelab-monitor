/**
 * Map a converged LogLine.severity to a Tailwind text-tint class.
 * Locked Design (STAGE-004-003): error/critical/alert/emergency -> red,
 * warn -> yellow, everything else (incl. null) -> '' (no tint).
 * No background colors — keeps the line list readable in bulk.
 */
const RED = new Set(['error', 'critical', 'alert', 'emergency'])

export function severityTintClass(severity: string | null | undefined): string {
  if (severity == null) return ''
  if (RED.has(severity)) return 'text-red-500'
  if (severity === 'warn') return 'text-yellow-500'
  return ''
}

/**
 * Hex bar-fill colors for the severity-stacked density histogram (STAGE-004-019).
 * Coarse keys match the backend (error/warn/info). Colors align with the Locked
 * Design: red=error+ (#ef4444), yellow=warn (#eab308), gray=info/other (#9ca3af).
 */
export const SEVERITY_BAR_COLORS: Record<'error' | 'warn' | 'info', string> = {
  error: '#ef4444',
  warn: '#eab308',
  info: '#9ca3af',
}

export function severityBarColor(coarse: 'error' | 'warn' | 'info'): string {
  return SEVERITY_BAR_COLORS[coarse]
}
