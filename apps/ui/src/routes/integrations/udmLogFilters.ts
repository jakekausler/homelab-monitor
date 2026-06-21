// STAGE-007-023 — Pure LogsQL expr builders for the UDM (UniFi gateway) syslog stream.
//
// UDM coverage is achieved via a `service:udm-*` wildcard in the `expr` STRING, with the
// backend `services` CSV left EMPTY (the CSV only does exact match per identity and cannot
// wildcard; the backend AND-joins the user expr). All UDM scoping therefore lives here.
//
// Stream facts (from the UDM syslog ingest):
//   source_type:"udm" always; service ∈ {udm-audit, udm-firewall, udm-system, udm-other}.
import { fieldFilterClause } from '@/lib/logsQlTranslate'

/** The category presets surfaced as header chips on the Logs tab. */
export type UdmLogCategory = 'all' | 'firewall' | 'audit' | 'system'

/** Ordered list for rendering chips (single source of truth for label/value). */
export const UDM_LOG_CATEGORIES: readonly { value: UdmLogCategory; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'firewall', label: 'Firewall' },
  { value: 'audit', label: 'Audit' },
  { value: 'system', label: 'System' },
] as const

/** Base scope clause per category. `all` uses the udm-* wildcard. */
function categoryScope(category: UdmLogCategory): string {
  switch (category) {
    case 'firewall':
      return 'source_type:udm service:udm-firewall'
    case 'audit':
      return 'source_type:udm service:udm-audit'
    case 'system':
      return 'source_type:udm service:udm-system'
    case 'all':
    default:
      return 'source_type:udm service:udm-*'
  }
}

/**
 * Optional client-IP filter, matching either source OR destination. Uses
 * fieldFilterClause for correct LogsQL escaping. Returns '' when ip is empty/whitespace
 * (fieldFilterClause returns null in that case).
 */
function ipClause(ip: string): string {
  const src = fieldFilterClause('src', ip)
  const dst = fieldFilterClause('dst', ip)
  if (src === null || dst === null) return ''
  return ` (${src} OR ${dst})`
}

/** Logs tab expr: category scope, optionally AND'd with a src/dst IP filter. */
export function buildUdmLogsExpr(category: UdmLogCategory, ip = ''): string {
  return `${categoryScope(category)}${ipClause(ip)}`
}

/**
 * Security Events tab expr: covers admin/audit actions and firewall blocks. IDS/IPS
 * detections arrive via the controller's structured alarm path, not this syslog stream.
 */
export function buildUdmThreatsExpr(ip = ''): string {
  return `source_type:udm (service:udm-audit OR service:udm-firewall)${ipClause(ip)}`
}
