/**
 * Wrap a bare LogsQL query into a count-threshold alert expr. If the query
 * already contains a `| stats` pipe (case-insensitive, with or without spaces),
 * it is returned unchanged (the user already authored a stats pipe). The
 * threshold (10) lives inside the string and is editable by editing the expr.
 */
export function scaffoldLogsqlExpr(query: string): string {
  const trimmed = query.trim()
  if (trimmed.length === 0) return ''
  // Match `| stats` or `|stats` (case-insensitive) anywhere in the query.
  if (/\|\s*stats/i.test(trimmed)) {
    return trimmed
  }
  return `${trimmed} | stats count() as match_count | filter match_count:>10`
}
