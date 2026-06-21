import type { Schema } from '@/api/types'

export type ClientRow = Schema<'UnifiClientRowModel'>
export type ClientSortKey = 'name' | 'ip' | 'mac' | 'network' | 'last_seen' | 'online'
export type SortDir = 'asc' | 'desc'

/** Display label for the name column: name, falling back to hostname, then mac. */
export function clientDisplayName(row: ClientRow): string {
  return row.name ?? row.hostname ?? row.mac
}

/** Connection summary from a ROW (table-level, not detail). */
export function clientConnection(row: ClientRow): string {
  if (row.ap_mac !== null) return `Wi-Fi (${row.ap_mac})`
  return 'Wired'
}

/**
 * Case-insensitive substring filter across name/hostname/ip/mac.
 * Empty/whitespace query returns the input unchanged. Pure.
 */
export function filterClients(rows: ClientRow[], query: string): ClientRow[] {
  const q = query.trim().toLowerCase()
  if (q.length === 0) return rows
  return rows.filter((r) => {
    const haystack = [r.name, r.hostname, r.ip, r.mac]
      .filter((v): v is string => v !== null)
      .join(' ')
      .toLowerCase()
    return haystack.includes(q)
  })
}

/** Stable sort by the given key/direction. Pure (operates on a copy). */
export function sortClients(rows: ClientRow[], key: ClientSortKey, dir: SortDir): ClientRow[] {
  const sign = dir === 'asc' ? 1 : -1
  const copy = [...rows]
  copy.sort((a, b) => {
    let cmp: number
    switch (key) {
      case 'name':
        cmp = clientDisplayName(a).localeCompare(clientDisplayName(b))
        break
      case 'ip':
        cmp = (a.ip ?? '').localeCompare(b.ip ?? '')
        break
      case 'mac':
        cmp = a.mac.localeCompare(b.mac)
        break
      case 'network':
        cmp = (a.network ?? '').localeCompare(b.network ?? '')
        break
      case 'last_seen':
        cmp = (a.last_seen ?? '').localeCompare(b.last_seen ?? '')
        break
      case 'online':
        cmp = Number(a.online) - Number(b.online)
        break
    }
    return cmp * sign
  })
  return copy
}
