/**
 * Truncate a sha256 digest for display. Preserves the algo prefix + first 12 hex chars.
 *
 * Examples:
 *   formatDigest("sha256:c5dd3503828713c4949ae1bccd1d8d69f382c33d441954674a6b78ebe69c3331")
 *     → "sha256:c5dd35038287…"
 *   formatDigest("nginx:1.27") → "nginx:1.27"  (not a digest, returned as-is)
 *   formatDigest(null) → "—"
 */
const DIGEST_PREFIX_LENGTH = 12

export function formatDigest(value: string | null | undefined): string {
  if (!value) return '—'
  // Match "sha256:<hex>" with hex of any length (typically 64 for full digest).
  const match = /^sha256:([0-9a-f]+)$/i.exec(value)
  const hex = match?.[1]
  if (!hex) return value
  if (hex.length <= DIGEST_PREFIX_LENGTH) return value
  return `sha256:${hex.slice(0, DIGEST_PREFIX_LENGTH)}…`
}
