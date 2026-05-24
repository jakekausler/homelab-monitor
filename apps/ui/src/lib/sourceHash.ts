/**
 * Format a build-context source hash for display.
 *
 * Handles local-build source hashes (raw hex or OVERSIZED sentinel).
 *
 * Examples:
 *   formatSourceHash("abc123def456abc123def456abc123def456abc123def456abc123def456abc1")
 *     → "abc123def456…"
 *   formatSourceHash("OVERSIZED:context_too_large")
 *     → "OVERSIZED:context_too_large"  (full sentinel preserved for operator)
 *   formatSourceHash(null) → "—"
 */
const SOURCE_HASH_PREFIX_LENGTH = 12

export function formatSourceHash(value: string | null | undefined): string {
  if (!value) return '—'

  // Preserve OVERSIZED sentinel completely (operator needs to see reason).
  if (value.startsWith('OVERSIZED:')) return value

  // For hex-looking hashes, truncate to first 12 chars + ellipsis.
  if (value.length >= SOURCE_HASH_PREFIX_LENGTH) {
    return `${value.slice(0, SOURCE_HASH_PREFIX_LENGTH)}…`
  }

  // Short hash (< 12 chars), return as-is.
  return value
}
