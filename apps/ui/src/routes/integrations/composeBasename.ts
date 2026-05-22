export function extractComposeBasename(path: string | null | undefined): string | null {
  if (!path) return null
  const parts = path.split('/')
  return parts.length >= 2 ? parts[parts.length - 2]! : parts[0]!
}
