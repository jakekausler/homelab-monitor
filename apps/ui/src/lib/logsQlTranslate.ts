// STAGE-004-010 — translate a plain-text search box into a LogsQL expression.
//
// Mirrors the backend's logsql_quote_phrase escape order
// (apps/monitor/homelab_monitor/kernel/logs/victorialogs_client.py):
// LogsQL quoted phrases are Go-style quoted strings, so the backslash is the
// escape introducer and MUST be escaped FIRST ("\\" -> "\\\\"), THEN the double
// quote ('"' -> '\\"'). Escaping the quote first would leave the inserted
// backslash unescaped.
export function translateSearchToLogsQl(searchText: string): string {
  const trimmed = searchText.trim()
  if (trimmed.length === 0) return '*'
  const escaped = trimmed.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  return `_msg:"${escaped}"`
}
