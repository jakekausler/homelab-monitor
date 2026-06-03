import type { ServiceIdentity } from '@/api/logs'
import { ALL_PRESETS, parseIso, type PresetToken, type TimeRangeValue } from '@/lib/timeRange'

export const STORAGE_KEY = 'homelab-monitor:logs-explorer-state'

/** Data attribute name for the app's main scroll container (AppShell <main>). */
export const SCROLL_CONTAINER_ATTR = 'data-app-scroll-container'

/** Data attribute name for the Logs Explorer's internal results scroll container
 *  (the vertically-scrolling region inside LogViewer). STAGE-015 scroll save/restore
 *  targets THIS element on the Explorer route, not the page-level <main>. */
export const LOG_SCROLL_CONTAINER_ATTR = 'data-log-scroll-container'

/** Seven days in milliseconds. Persisted state older than this is discarded on load. */
export const EXPLORER_STATE_TTL_MS = 7 * 24 * 60 * 60 * 1000

const DEFAULT_PRESET: PresetToken = '1h'

/** Persisted Explorer state. Mirrors serializeCurrentExplorerState()'s payload
 *  fields (mode/logs_ql/services/range) so save/load round-trips, plus scroll +
 *  bookkeeping. Range is EITHER since_preset OR (range_start_iso AND range_end_iso),
 *  matching the HistoryEntry/SaveQueryCreateRequest convention. */
export interface ExplorerState {
  /** Advanced (raw LogsQL) mode vs plain-text mode. */
  advanced_mode: boolean
  /** The COMMITTED query text for the active mode at save time.
   *  Plain mode → the plain text; advanced mode → the raw LogsQL. Mirrors
   *  serializeCurrentExplorerState's `logs_ql` field (which is the active mode's
   *  committed text, NOT necessarily LogsQL syntax in plain mode). */
  logs_ql: string
  /** Selected service identities (same shape the page state uses). */
  selected_services: ServiceIdentity[]
  /** Range: preset token (mutually exclusive with custom bounds). */
  since_preset?: string | null
  /** Range: custom-window ISO bounds (both present together, or both absent). */
  range_start_iso?: string | null
  range_end_iso?: string | null
  /** Vertical scrollTop (px) of the log <pre> at save time. v1: naive pixel restore. */
  scroll_position?: number | null
  /** Forward-compat (per card): pagination cursor. v1 does NOT resume from it. */
  cursor?: string | null
  /** Unix epoch ms of the last save (Date.now()). Drives the 7-day TTL. */
  last_visited_at: number
}

/** Fully-normalized seed the Page consumes to initialize useState. */
export interface ExplorerSeed {
  advancedMode: boolean
  /** Committed plain-text (empty string when advanced mode). */
  plainText: string
  /** Committed LogsQL (empty string when plain mode). */
  logsQl: string
  range: TimeRangeValue
  selectedIdentities: ServiceIdentity[]
  /** The scrollTop to restore, or null when NOT restoring (URL won, no persisted,
   *  fresh visit, or persisted scroll <= 0). The Page passes this to Body as
   *  `restoreScrollTarget`. */
  restoreScrollTarget: number | null
}

function isPresetToken(s: string): s is PresetToken {
  return (ALL_PRESETS as readonly string[]).includes(s)
}

/** Read raw persisted state. SSR-safe. Corrupt / missing / wrong-shape → null.
 *  Does NOT apply the TTL (that is loadExplorerState's job). */
function readState(): ExplorerState | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === null) return null
    const parsed: unknown = JSON.parse(raw)
    if (
      typeof parsed !== 'object' ||
      parsed === null ||
      typeof (parsed as ExplorerState).advanced_mode !== 'boolean' ||
      typeof (parsed as ExplorerState).logs_ql !== 'string' ||
      !Array.isArray((parsed as ExplorerState).selected_services) ||
      typeof (parsed as ExplorerState).last_visited_at !== 'number'
    ) {
      return null
    }
    return parsed as ExplorerState
  } catch {
    return null
  }
}

/** Persist state. SSR-safe. setItem failure (quota) is silently ignored. */
function writeState(state: ExplorerState): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Quota / serialize failure is non-fatal for disposable UI state; ignore.
  }
}

/** Public save: write the full state object verbatim. */
export function saveExplorerState(state: ExplorerState): void {
  writeState(state)
}

/** Public load: read + apply 7-day TTL. Returns null when missing, corrupt, or
 *  expired (Date.now() - last_visited_at > EXPLORER_STATE_TTL_MS). */
export function loadExplorerState(): ExplorerState | null {
  const state = readState()
  if (state === null) return null
  if (Date.now() - state.last_visited_at > EXPLORER_STATE_TTL_MS) return null
  return state
}

/** Read-modify-write merge. load() ?? {} → {...prev, ...partial,
 *  last_visited_at: Date.now()} → save(). Prevents the two writers (query-save
 *  effect + debounced scroll-save) from clobbering each other. The TTL inside
 *  loadExplorerState means an EXPIRED prev is treated as absent (fresh base). */
export function patchExplorerState(partial: Partial<ExplorerState>): void {
  const base: Partial<ExplorerState> = loadExplorerState() ?? {}
  // Build the merged object. Required fields come from partial-or-prev; the
  // merge guarantees advanced_mode/logs_ql/selected_services exist because the
  // first writer (query-save effect) always patches them.
  const merged = {
    ...base,
    ...partial,
    last_visited_at: Date.now(),
  } as ExplorerState
  saveExplorerState(merged)
}

/** URL params as the Page reads them (all optional strings; services pre-parsed). */
export interface ExplorerUrlParams {
  q?: string | undefined
  logsql?: string | undefined
  since?: string | undefined
  start?: string | undefined
  end?: string | undefined
  services?: ServiceIdentity[] | undefined
}

/** Build a TimeRangeValue from custom ISO bounds OR a preset token OR default.
 *  Mirrors initialRange (URL) + reconstructFromPayload's range branch. */
function rangeFromBounds(
  sincePreset: string | null | undefined,
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): TimeRangeValue {
  const start = startIso != null ? parseIso(startIso) : null
  const end = endIso != null ? parseIso(endIso) : null
  if (start !== null || end !== null) {
    return {
      kind: 'custom',
      ...(start !== null ? { start } : {}),
      ...(end !== null ? { end } : {}),
    }
  }
  if (sincePreset != null && isPresetToken(sincePreset)) {
    return { kind: 'preset', token: sincePreset }
  }
  return { kind: 'preset', token: DEFAULT_PRESET }
}

/** The default seed (fresh visit, no URL, no/expired persisted). */
function defaultSeed(): ExplorerSeed {
  return {
    advancedMode: false,
    plainText: '',
    logsQl: '',
    range: { kind: 'preset', token: DEFAULT_PRESET },
    selectedIdentities: [],
    restoreScrollTarget: null,
  }
}

/** PURE precedence resolver. ALL-OR-NOTHING:
 *   1. If URL has ANY of q/logsql/since/start/end/services → seed from URL
 *      ENTIRELY (persisted ignored; restoreScrollTarget = null).
 *   2. Else if persisted != null (already TTL-checked by caller via
 *      loadExplorerState) → seed from persisted, restoreScrollTarget =
 *      scroll_position (when > 0, else null).
 *   3. Else → default seed.
 *
 *  Caller MUST pass loadExplorerState() as `persisted` so the TTL is applied. */
export function resolveInitialExplorerState(
  url: ExplorerUrlParams,
  persisted: ExplorerState | null,
): ExplorerSeed {
  const urlHasAny =
    url.q !== undefined ||
    url.logsql !== undefined ||
    url.since !== undefined ||
    url.start !== undefined ||
    url.end !== undefined ||
    (url.services !== undefined && url.services.length > 0)

  if (urlHasAny) {
    const advancedMode = url.logsql !== undefined
    return {
      advancedMode,
      plainText: url.q ?? '',
      logsQl: url.logsql ?? '',
      range: rangeFromBounds(url.since, url.start, url.end),
      selectedIdentities: url.services ?? [],
      restoreScrollTarget: null, // URL drove the query → persisted scroll is stale.
    }
  }

  if (persisted !== null) {
    const advancedMode = persisted.advanced_mode
    const scroll = persisted.scroll_position
    return {
      advancedMode,
      plainText: advancedMode ? '' : persisted.logs_ql,
      logsQl: advancedMode ? persisted.logs_ql : '',
      range: rangeFromBounds(
        persisted.since_preset,
        persisted.range_start_iso,
        persisted.range_end_iso,
      ),
      selectedIdentities: persisted.selected_services,
      restoreScrollTarget: typeof scroll === 'number' && scroll > 0 ? scroll : null,
    }
  }

  return defaultSeed()
}
