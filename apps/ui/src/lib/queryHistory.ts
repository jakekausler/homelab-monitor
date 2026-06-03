export const STORAGE_KEY = 'homelab-monitor:logs-query-history'

const MAX_ENTRIES = 20

/** One recorded query. Payload-shaped (same fields as SaveQueryCreateRequest
 *  minus `name`) plus a record timestamp. `resultCount` reserved for future. */
export interface HistoryEntry {
  /** Stable unique identifier (UUID). */
  id: string
  /** Unix epoch milliseconds the query was committed (Date.now()). */
  timestamp: number
  advanced_mode: boolean
  logs_ql: string
  selected_services: { service: string; source_type: string }[]
  /** Range: EITHER since_preset OR (range_start_iso AND range_end_iso). */
  since_preset?: string | null
  range_start_iso?: string | null
  range_end_iso?: string | null
  /** Reserved for v2; never set in v1. */
  resultCount?: number
}

/** Stable equality key for consecutive-dedupe. EXCLUDES timestamp + resultCount.
 *  selected_services is sorted so order doesn't matter. */
export function equalityKey(entry: HistoryEntry): string {
  const services = entry.selected_services
    .map((s) => `${s.source_type}:${s.service}`)
    .slice()
    .sort()
  return JSON.stringify({
    advanced_mode: entry.advanced_mode,
    logs_ql: entry.logs_ql,
    services,
    since_preset: entry.since_preset ?? null,
    range_start_iso: entry.range_start_iso ?? null,
    range_end_iso: entry.range_end_iso ?? null,
  })
}

/** PURE: given the current list (most-recent-first) and a new entry, return the
 *  next list. Consecutive-identical (new key === list[0] key) → replace the top
 *  entry's timestamp with the new entry's timestamp, no new row. Otherwise
 *  prepend and cap at MAX_ENTRIES (oldest = last, rolls off). */
export function appendWithDedupeAndCap(list: HistoryEntry[], entry: HistoryEntry): HistoryEntry[] {
  if (list.length > 0 && equalityKey(list[0]!) === equalityKey(entry)) {
    const next = list.slice()
    next[0] = { ...next[0]!, timestamp: entry.timestamp }
    return next
  }
  return [entry, ...list].slice(0, MAX_ENTRIES)
}

/** Read the persisted history. SSR-safe. Corrupt/missing/non-array JSON → []. */
export function readHistory(): HistoryEntry[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === null) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Validate per-entry shape: require id, timestamp, and selected_services
    return (parsed as unknown[]).filter(
      (e): e is HistoryEntry =>
        typeof e === 'object' &&
        e !== null &&
        typeof (e as HistoryEntry).id === 'string' &&
        typeof (e as HistoryEntry).timestamp === 'number' &&
        Array.isArray((e as HistoryEntry).selected_services),
    )
  } catch {
    return []
  }
}

/** Persist + notify same-tab subscribers. SSR-safe. */
export function writeHistory(entries: HistoryEntry[]): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(entries))
  } catch {
    // Quota/serialize failure is non-fatal for a disposable history; ignore.
  }
  emit()
}

/** Record a committed query: read → pure-append → write → emit. */
export function recordQuery(entry: HistoryEntry): HistoryEntry[] {
  const next = appendWithDedupeAndCap(readHistory(), entry)
  writeHistory(next)
  return next
}

/** Wipe history + notify subscribers. */
export function clearHistory(): void {
  writeHistory([])
}

// --- Same-tab pub-sub -------------------------------------------------------
// localStorage 'storage' events DO NOT fire in the same tab that wrote them, so
// the page's recordQuery() won't notify the panel's useQueryHistory() instance.
// This module-level listener set fixes that: writeHistory()/clearHistory() emit;
// useQueryHistory subscribes and re-reads on emit.
type Listener = () => void
const listeners = new Set<Listener>()

export function subscribe(cb: Listener): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

function emit(): void {
  for (const cb of listeners) cb()
}
