// STAGE-004-010 — translate a plain-text search box into a LogsQL expression.
//
// Mirrors the backend's logsql_quote_phrase escape order
// (apps/monitor/homelab_monitor/kernel/logs/victorialogs_client.py):
// LogsQL quoted phrases are Go-style quoted strings, so the backslash is the
// escape introducer and MUST be escaped FIRST ("\\" -> "\\\\"), THEN the double
// quote ('"' -> '\\"'). Escaping the quote first would leave the inserted
// backslash unescaped.

/** Escape a raw string for use inside a LogsQL quoted phrase.
 *  Backslash FIRST (it is the escape introducer), THEN the double quote. */
export function escapeLogsQlPhrase(raw: string): string {
  return raw.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
}

/** Translate a plain-text search box value into a LogsQL expression.
 *  Empty/whitespace-only → match-all '*'. */
export function translateSearchToLogsQl(searchText: string): string {
  const trimmed = searchText.trim()
  if (trimmed.length === 0) return '*'
  return `_msg:"${escapeLogsQlPhrase(trimmed)}"`
}

/** Translate a single add-to-filter value into a discrete _msg:"…" LogsQL clause.
 *  Returns null if value is empty/whitespace. Used by appendMsgFilter to compose
 *  ANDed discrete substring filters in advanced mode. */
export function msgFilterClause(value: string): string | null {
  const trimmed = value.trim()
  if (trimmed.length === 0) return null
  return `_msg:"${escapeLogsQlPhrase(trimmed)}"`
}

/** Translate a Drain template string (with `<*>` wildcards) into a LogsQL filter
 *  for "Open in Explorer". STAGE-004-031A Refinement.
 *
 *  Strategy: anchor on the SINGLE LONGEST literal run between wildcards rather
 *  than AND-chaining every inter-wildcard segment. AND-chaining over-constrains
 *  the match (every fragment must match contiguously) and is fragile when a
 *  trailing fragment carries non-printable bytes (e.g. an ANSI reset `\x1b[0m`),
 *  which collapse the whole conjunction to zero results. The longest run is the
 *  most distinctive, stable identifier of the signature and reliably matches.
 *  Returns '*' (match-all) when the template has no usable literal run. */
export function templateToLogsQl(templateStr: string): string {
  const longest = templateStr
    .split('<*>')
    .reduce((best, seg) => (seg.trim().length > best.trim().length ? seg : best), '')
  // All-wildcard template (no literal run): fall back to match-all so "Open in
  // Explorer" still opens the Explorer (showing all logs in range) rather than an
  // empty/invalid query. Callers keep the button visible for this case.
  if (longest.trim().length === 0) return '*'
  return `_msg:"${escapeLogsQlPhrase(longest.trim())}"`
}

/** Translate a field name + value into a discrete `field:"value"` LogsQL clause.
 *  Returns null if value is empty/whitespace. Used by appendFieldFilter to compose
 *  ANDed structured field filters in advanced mode.
 *  Field names from the inspector are known-safe identifiers (dots allowed in LogsQL). */
export function fieldFilterClause(field: string, value: string): string | null {
  const trimmed = value.trim()
  if (trimmed.length === 0) return null
  return `${field}:"${escapeLogsQlPhrase(trimmed)}"`
}
