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
