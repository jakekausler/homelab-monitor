// STAGE-004-010 — translate a plain-text search box into a LogsQL expression.
//
// Mirrors the backend's logsql_quote_phrase escape order
// (apps/monitor/homelab_monitor/kernel/logs/victorialogs_client.py):
// LogsQL quoted phrases are Go-style quoted strings, so the backslash is the
// escape introducer and MUST be escaped FIRST ("\\" -> "\\\\"), THEN the double
// quote ('"' -> '\\"'). Escaping the quote first would leave the inserted
// backslash unescaped.

/** Escape a raw string for use inside a LogsQL quoted phrase. */
function escapeLogsQlPhrase(raw: string): string {
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
