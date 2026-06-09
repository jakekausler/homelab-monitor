import { templateToLogsQl, escapeLogsQlPhrase } from '@/lib/logsQlTranslate'
import { scaffoldLogsqlExpr } from '@/components/logs/alertExpr'
import type { CreateAlertFormValues } from '@/components/logs/CreateAlertModal'
import type { SignatureResponse } from '@/api/signatures'
import type { SavedQuery } from '@/api/savedLogQueries'

export interface AlertPrefill {
  initialMode: 'simple' | 'advanced'
  initialValues: Partial<CreateAlertFormValues>
  sourceKind: string
  sourceRef: string
}

/**
 * Mirror of the backend's _SERVICES_MAX_LIMIT constant in
 * apps/monitor/homelab_monitor/kernel/api/routers/logs.py (line 169).
 * Keep in sync if the backend cap changes.
 */
const _SERVICES_MAX_LIMIT = 1000

/**
 * Replicate the backend's _compose_services_expr logic on the frontend so that
 * an alert created from a service-scoped saved query fires on the SAME log lines
 * the Explorer shows.
 *
 * Clause shape mirrors the backend exactly:
 *   service:"<val>" AND source_type:"<val>"  (no = prefix — matches backend form)
 *
 * Single identity:   (service:"x" AND source_type:"y") AND (<baseExpr>)
 * Multiple:          ((service:"a" AND source_type:"y") OR (service:"b" AND source_type:"z")) AND (<baseExpr>)
 * Empty services:    baseExpr unchanged
 * Empty baseExpr:    just the service clause (no trailing AND ()) so the result
 *                    is valid LogsQL
 * Malformed entries (empty service or source_type) are skipped, matching the backend.
 */
export function composeServicesExpr(
  baseExpr: string,
  services: ReadonlyArray<{ service: string; source_type: string }>,
): string {
  if (services.length === 0) return baseExpr

  const clauses: string[] = []
  for (const { service, source_type } of services) {
    if (!service || !source_type) continue // skip malformed, matching backend
    const svcQ = `"${escapeLogsQlPhrase(service)}"`
    const stQ = `"${escapeLogsQlPhrase(source_type)}"`
    clauses.push(`service:${svcQ} AND source_type:${stQ}`)
    if (clauses.length >= _SERVICES_MAX_LIMIT) break
  }

  if (clauses.length === 0) return baseExpr

  const trimmedBase = baseExpr.trim()

  if (clauses.length === 1) {
    if (!trimmedBase) return `(${clauses[0]})`
    return `(${clauses[0]}) AND (${trimmedBase})`
  }

  const orClause = clauses.map((c) => `(${c})`).join(' OR ')
  if (!trimmedBase) return `(${orClause})`
  return `(${orClause}) AND (${trimmedBase})`
}

/**
 * Produce a DETERMINISTIC rule_name satisfying /^[a-zA-Z_][a-zA-Z0-9_]*$/.
 *
 * Algorithm:
 *   raw  = `${prefix}_${namePart}_${idPart}`
 *   safe = raw.replace(/[^a-zA-Z0-9_]/g, '_')
 *   if safe[0] is not a letter or underscore → prepend 'r_'
 *   truncate to 200 chars
 *
 * Determinism: same inputs → same output (enables dup-name detection).
 * No consecutive-underscore collapsing (RULE_NAME_REGEX allows them).
 */
export function deriveRuleName(prefix: string, idPart: string, namePart: string): string {
  const raw = `${prefix}_${namePart}_${idPart}`
  let safe = raw.replace(/[^a-zA-Z0-9_]/g, '_')
  if (!/^[a-zA-Z_]/.test(safe)) {
    safe = `r_${safe}`
  }
  // rule_name max is 200 chars (formSchema enforces this)
  return safe.slice(0, 200)
}

/**
 * Build CreateAlertModal prefill for a signature — always Advanced mode (logsql
 * count-threshold expr derived from the template).
 */
export function prefillFromSignature(sig: SignatureResponse): AlertPrefill {
  const logsQl = templateToLogsQl(sig.template_str)
  const expr = scaffoldLogsqlExpr(logsQl)
  return {
    initialMode: 'advanced',
    initialValues: {
      expr,
      expr_kind: 'logsql',
      rule_name: deriveRuleName('SignatureSpike', sig.template_hash, sig.service_key),
      severity: 'warning',
      for_duration: '1m',
      summary: `Spike in signature ${sig.template_hash} from ${sig.service_key}`,
      description: `Count-threshold alert for log signature ${sig.template_hash} (service: ${sig.service_key}). Fires when the pattern match count exceeds 10 within 1 minute.`,
    },
    sourceKind: 'signature',
    sourceRef: `${sig.template_hash}:${sig.service_key}`,
  }
}

/**
 * Build CreateAlertModal prefill for a saved query.
 *
 * Service-scoped (selected_services non-empty) → Always Advanced mode with
 * composed service + logs_ql expr (replicates backend _compose_services_expr).
 *
 * No services + Advanced mode → Advanced mode with scaffolded logs_ql expr.
 * No services + Simple mode   → Simple mode with simple_contains (modal derives expr).
 *
 * Mirrors the 043 Explorer launch pattern exactly.
 */
export function prefillFromSavedQuery(sq: SavedQuery): AlertPrefill {
  const ruleName = deriveRuleName('SavedQuery', String(sq.id), sq.name)
  const summary = `Saved query "${sq.name}" matched > 10 lines`
  const description = `Count-threshold alert derived from saved query "${sq.name}" (id: ${sq.id}).`

  const hasServices = sq.selected_services.length > 0

  if (hasServices) {
    // Always Advanced mode when services are scoped — Simple mode cannot express
    // multi-service + source_type constraints (documented Tier-2 deferral).
    // Compose the service scope onto the raw logs_ql FIRST, then scaffold the
    // stats pipe so the final expr is:
    //   (<services>) AND (<logs_ql>) | stats count() as match_count | filter match_count:>10
    // Empty logs_ql is handled by composeServicesExpr (returns just the services
    // clause, no trailing AND ()).
    const composed = composeServicesExpr(sq.logs_ql, sq.selected_services)
    const expr = scaffoldLogsqlExpr(composed)
    return {
      initialMode: 'advanced',
      initialValues: {
        expr,
        expr_kind: 'logsql',
        rule_name: ruleName,
        severity: 'warning',
        for_duration: '5m',
        summary,
        description,
      },
      sourceKind: 'saved_query',
      sourceRef: String(sq.id),
    }
  }

  if (sq.advanced_mode) {
    return {
      initialMode: 'advanced',
      initialValues: {
        expr: scaffoldLogsqlExpr(sq.logs_ql),
        expr_kind: 'logsql',
        rule_name: ruleName,
        severity: 'warning',
        for_duration: '5m',
        summary,
        description,
      },
      sourceKind: 'saved_query',
      sourceRef: String(sq.id),
    }
  }

  // Simple mode: pre-fill simple_contains only (no expr — modal derives it)
  return {
    initialMode: 'simple',
    initialValues: {
      simple_contains: sq.logs_ql,
      expr_kind: 'logsql',
      rule_name: ruleName,
      severity: 'warning',
      for_duration: '5m',
      summary,
      description,
    },
    sourceKind: 'saved_query',
    sourceRef: String(sq.id),
  }
}
