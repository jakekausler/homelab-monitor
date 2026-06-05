import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Activity, Filter, RefreshCw, Save, Search, X } from 'lucide-react'

import { ApiError } from '@/api/client'
import {
  fetchNewerLogs,
  identitiesToServicesCsv,
  useLogsQuery,
  useLogsServicesQuery,
  type ServiceIdentity,
} from '@/api/logs'
import { type SavedQuery } from '@/api/savedLogQueries'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet'
import { AdvancedToggle, WrapIconToggle } from '@/components/logs/AdvancedToggle'
import { FieldInspectorPanel } from '@/components/logs/FieldInspectorPanel'
import { LogViewer } from '@/components/logs/LogViewer'
import { LogsQlEditor } from '@/components/logs/LogsQlEditor'
import { StreamPickerSidebar } from '@/components/logs/StreamPickerSidebar'
import { SavedQueriesPanel } from './SavedQueriesPanel'
import { QueryHistoryPanel } from './QueryHistoryPanel'
import { FieldsDiscoveryPanel } from './FieldsDiscoveryPanel'
import { HistogramChart } from './HistogramChart'
import { ExportButton } from './ExportButton'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { translateSearchToLogsQl } from '@/lib/logsQlTranslate'
import { cn } from '@/lib/utils'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useTimezonePreference } from '@/lib/useTimezonePreference'
import { patchExplorerState, LOG_SCROLL_CONTAINER_ATTR } from '@/lib/explorerState'
import { useWindowedLogs, RENDER_CAP } from '@/lib/useWindowedLogs'
import { useLogsTail } from '@/lib/logsTail'
import { parseIso } from '@/lib/timeRange'
import type { HistoryEntry } from '@/lib/queryHistory'
import {
  ALL_PRESETS,
  resolveCustomWindow,
  resolvePreset,
  toIsoZ,
  type TimeRangeValue,
} from '@/lib/timeRange'
import type { LogLine, LogViewerStatus, UseLogsResult } from '@/components/logs/types'

const EMPTY_COPY = 'No matches in the selected range. Try a wider time range or a different query.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

interface LogsExplorerBodyProps {
  /** Advanced (raw LogsQL) mode vs plain-text mode. */
  advancedMode: boolean
  /** COMMITTED plain-text search (reflected in the URL ?q when plain mode). */
  committedPlainText: string
  /** Live (uncommitted) plain-text input value. */
  livePlainText: string
  /** COMMITTED raw LogsQL (reflected in the URL ?logsql when advanced mode). */
  committedLogsQl: string
  /** Live (uncommitted) LogsQL editor value. */
  liveLogsQl: string
  /** Committed time range (mirrors the URL). */
  range: TimeRangeValue
  /** Selected service identities (AND'd server-side by source_type:service). */
  selectedIdentities: ServiceIdentity[]
  /** Update the live plain-text input (no query/URL change). */
  onLivePlainTextChange: (next: string) => void
  /** Update the live LogsQL editor text (no query/URL change). */
  onLiveLogsQlChange: (next: string) => void
  /** Flip advanced/plain mode (preserves both texts; rewrites the URL). */
  onToggleAdvanced: (next: boolean) => void
  /** Commit the active mode's live text → updates URL + triggers the query. */
  onSubmitSearch: () => void
  /** Clear the ACTIVE mode's text (commits empty → omits that URL key). */
  onClearSearch: () => void
  /** Range picker change → Page writes URL (since OR start/end). */
  onRangeChange: (next: TimeRangeValue) => void
  /** Toggle an identity in/out of the selection (row click + chip ×). */
  onToggleIdentity: (identity: ServiceIdentity) => void
  /** STAGE-004-016 fix: add an identity to the selection (additive only, no
   *  toggle-remove). Used by the inspector's + button. */
  onAddIdentity: (identity: ServiceIdentity) => void
  /** Bulk add identities to the selection. */
  onSelectIdentities: (identities: ServiceIdentity[]) => void
  /** Bulk remove identities from the selection. */
  onDeselectIdentities: (identities: ServiceIdentity[]) => void
  /** Open the save-query modal (page owns the modal + payload builder). */
  onOpenSave: () => void
  /** Load a saved query into the Explorer (page reconstructs state). */
  onLoadSavedQuery: (saved: SavedQuery) => void
  /** Overwrite a saved query's payload with the current Explorer state. */
  onUpdateSavedQuery: (saved: SavedQuery) => void
  /** Load a recent (history) query into the Explorer (page reconstructs state). */
  onLoadHistoryEntry: (entry: HistoryEntry) => void
  /** The persisted scrollTop to restore once results render, or null/undefined to
   *  skip restore (URL took precedence, no persisted state, or fresh visit).
   *  STAGE-004-015. */
  restoreScrollTarget?: number | null
  /** STAGE-004-016: append a plain-text substring to the committed search
   *  (routes through writeUrl). Page provides; enables add-to-filter. */
  onAddMsgFilter: (value: string) => void
  /** STAGE-004-016A: append a structured field:"value" clause to the committed
   *  LogsQL query (routes through writeUrl). Page provides; enables add-to-filter
   *  for host/severity/bag fields. */
  onAddFieldFilter?: (field: string, value: string) => void
  /** STAGE-004-019: narrow the range to a clicked histogram bucket
   *  [startIso, endIso). Page commits a custom absolute range via writeUrl. */
  onNarrowRange: (startIso: string, endIso: string) => void
}

export function LogsExplorerBody({
  advancedMode,
  committedPlainText,
  livePlainText,
  committedLogsQl,
  liveLogsQl,
  range,
  selectedIdentities,
  onLivePlainTextChange,
  onLiveLogsQlChange,
  onToggleAdvanced,
  onSubmitSearch,
  onClearSearch,
  onRangeChange,
  onToggleIdentity,
  onAddIdentity,
  onSelectIdentities,
  onDeselectIdentities,
  onOpenSave,
  onLoadSavedQuery,
  onUpdateSavedQuery,
  onLoadHistoryEntry,
  restoreScrollTarget,
  onAddMsgFilter,
  onAddFieldFilter,
  onNarrowRange,
}: LogsExplorerBodyProps) {
  const [wrap, setWrap] = useState(false)
  // STAGE-004-009 timezone wiring (mirrors the Docker viewer).
  const [timezone, toggleTimezone] = useTimezonePreference()
  // Bumping this re-resolves the window against a fresh "now" (Refresh / live-tail
  // groundwork) WITHOUT churning the query key on every render.
  const [refreshNonce, setRefreshNonce] = useState(0)
  // Sidebar tab state: show Services, Saved queries, History, or Fields
  const [sidebarTab, setSidebarTab] = useState<'services' | 'saved' | 'history' | 'fields'>(
    'services',
  )
  // STAGE-004-016 fix: single source of truth for row selection.
  // Holds both the key (for highlight) and the line (for the inspector panel).
  // Clearing this closes the panel AND removes the row highlight atomically.
  const [selection, setSelection] = useState<{ key: string; line: LogLine } | null>(null)

  // STAGE-004-024: live tail state + refs
  const [isTailing, setIsTailing] = useState(false)
  const [sticky, setSticky] = useState(true)
  const [frozen, setFrozen] = useState(false)
  const [isLoadingNewer, setIsLoadingNewer] = useState(false)
  const [loadNewerError, setLoadNewerError] = useState<string | null>(null)
  // Latest-value refs read inside the scroll listener (which captures stale state).
  const isTailingRef = useRef(false)
  const stickyRef = useRef(true)
  // Windowed buffer + page/query-key diff tracking
  const windowed = useWindowedLogs()
  const prevPagesLenRef = useRef(0)
  const prevQueryKeyRef = useRef<string>('')
  const linesRef = useRef(windowed.state.lines)
  const HEADROOM = 200

  // Active mode decides the expr: advanced sends the COMMITTED raw LogsQL
  // verbatim (empty → match-all '*' to keep the always-enabled invariant);
  // plain translates the committed text into _msg:"…".
  const expr = advancedMode
    ? committedLogsQl.trim().length > 0
      ? committedLogsQl
      : '*'
    : translateSearchToLogsQl(committedPlainText)

  // Resolve the committed range to absolute [startIso, endIso]. `now` must stay
  // STABLE across renders (else the open-end window re-reads new Date() each
  // render → query-key churn → refetch loop). Memoize on the committed range +
  // refreshNonce so `now` only advances when the user changes the range or hits
  // Refresh. Mirrors DockerContainerLogsViewerBody's useMemo pattern.
  const { startIso, endIso } = useMemo(() => {
    const now = new Date()
    const win =
      range.kind === 'preset'
        ? resolvePreset(range.token, now)
        : resolveCustomWindow({ start: range.start, end: range.end }, { now, maxSpanDays: 30 })
    return { startIso: toIsoZ(win.start), endIso: toIsoZ(win.end) }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: re-resolve only on committed range change or explicit refresh
  }, [
    range.kind,
    range.kind === 'preset' ? range.token : undefined,
    range.kind === 'custom' ? range.start?.getTime() : undefined,
    range.kind === 'custom' ? range.end?.getTime() : undefined,
    refreshNonce,
  ])

  // The query is ALWAYS enabled here: expr is never empty (an empty search box
  // resolves to '*'), and startIso/endIso are always non-empty ISO strings. Do
  // NOT add a redundant empty-guard — useLogsQuery's `enabled` is effectively
  // always true for this consumer by design.
  const servicesCsv = identitiesToServicesCsv(selectedIdentities)
  const logs = useLogsQuery(expr, startIso, endIso, servicesCsv)

  const handleTailLines = useCallback(
    (batch: typeof windowed.state.lines) => {
      windowed.appendNewer(batch)
    },
    [windowed],
  )
  const tail = useLogsTail(expr, servicesCsv, { enabled: isTailing, onLines: handleTailLines })

  // Services query — depends on window ONLY, window-only refetch.
  const servicesQuery = useLogsServicesQuery(startIso, endIso)
  const servicesData = servicesQuery.data

  // Mobile detection + sidebar open state.
  const isMobile = useMediaQuery('(max-width: 767px)')
  // Sidebar (filters/services + saved) is CLOSED by default on both breakpoints.
  // The Filter button toggles it: desktop pushes an inline <aside>; mobile opens
  // a left drawer (Sheet).
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // STAGE-004-016 fix: Escape closes the desktop inspector. Mobile Sheet
  // handles Escape natively (Radix); this covers the desktop <aside> path.
  // Clears the single-source selection (key + line) so the highlight also clears.
  useEffect(() => {
    if (selection === null || isMobile) return
    const handleKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        setSelection(null)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [selection, isMobile])

  // STAGE-004-024: keep refs in sync for stale-closure fix
  useEffect(() => {
    isTailingRef.current = isTailing
  }, [isTailing])
  useEffect(() => {
    stickyRef.current = sticky
  }, [sticky])
  useLayoutEffect(() => {
    linesRef.current = windowed.state.lines
  }, [windowed.state.lines])

  // STAGE-004-015 — scroll persistence. RE-POINTED: the vertical scroll container
  // is now the Explorer's internal results region (LogViewer's
  // data-log-scroll-container), NOT the page-level <main> (which no longer scrolls
  // on this route — the panels scroll internally). We resolve and track THAT
  // element's scrollTop.
  const scrollSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // True until we've restored the persisted scroll once. Initialized from the
  // Page-resolved target so URL-precedence (target == null) means "never restore".
  const pendingRestoreRef = useRef<boolean>(restoreScrollTarget != null && restoreScrollTarget > 0)

  // Resolve the Logs Explorer's internal results scroll container (inside
  // LogViewer). Returns null in SSR/jsdom-without-DOM, and during the brief
  // window before the results region mounts. STAGE-015 (re-pointed from <main>).
  const getScrollContainer = (): HTMLElement | null => {
    if (typeof document === 'undefined') return null
    return document.querySelector<HTMLElement>(`[${LOG_SCROLL_CONTAINER_ATTR}]`)
  }

  // Debounced (200ms) scroll-save. Reads the main scroll container's scrollTop,
  // patches scroll_position only. patchExplorerState's read-modify-write preserves
  // the query fields. While tailing, track sticky instead of saving.
  const handleScroll = (): void => {
    const el = getScrollContainer()
    if (el === null) return
    // While tailing: track sticky (near-bottom) and SKIP the historical scroll-save.
    if (isTailingRef.current) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100
      setSticky(nearBottom)
      return
    }
    const top = el.scrollTop
    if (scrollSaveTimer.current !== null) clearTimeout(scrollSaveTimer.current)
    scrollSaveTimer.current = setTimeout(() => {
      patchExplorerState({ scroll_position: top })
    }, 200)
  }

  // Attach the scroll listener to the app's main scroll container; clean up the
  // listener + any pending debounce on unmount.
  useEffect(() => {
    const el = getScrollContainer()
    el?.addEventListener('scroll', handleScroll, { passive: true })
    return () => {
      el?.removeEventListener('scroll', handleScroll)
      if (scrollSaveTimer.current !== null) clearTimeout(scrollSaveTimer.current)
    }

    // (handleScroll is stable; we intentionally attach once on mount)
  }, [])

  const handleRefresh = (): void => {
    setFrozen(false)
    setRefreshNonce((n) => n + 1)
    void logs.refetch()
  }

  // Backend surfaces VictoriaLogs unavailability as HTTP 502 upstream_unavailable
  // (see apps/monitor/.../api/routers/logs.py). There is NO 503/404 on this
  // endpoint. Everything else non-2xx is a generic error.
  const isUnavailable = logs.error instanceof ApiError && logs.error.status === 502
  const isGenericApiError = logs.error instanceof ApiError && !isUnavailable

  const pages = logs.data?.pages ?? []
  // pages[0] is the NEWEST window; reverse so oldest renders first (mirrors the
  // Docker viewer). LogsQueryResponse has NO log_status/truncated fields, so we
  // derive logStatus below from line presence.
  const flatLines = useMemo(
    () =>
      pages
        .slice()
        .reverse()
        .flatMap((p) => p.lines),
    [pages],
  )
  const hasData = logs.data !== undefined

  // Restore persisted scroll ONCE, after results render (windowed buffer populated) so
  // the container is tall enough to scroll. Gated by restoreScrollTarget (null when
  // URL took precedence / no persisted state). After restoring, only saving happens.
  // STAGE-004-018B: variable row heights (configurable columns) may invalidate this
  // pixel offset — a future line-anchor restore would be more robust.
  // STAGE-004-024: live-tail auto-scroll will conflict — that stage must suppress
  // this restore while tailing.
  useLayoutEffect(() => {
    const el = getScrollContainer()
    if (
      !isTailing &&
      pendingRestoreRef.current &&
      restoreScrollTarget != null &&
      restoreScrollTarget > 0 &&
      windowed.state.lines.length > 0 &&
      el !== null
    ) {
      if (typeof el.scrollTo === 'function') el.scrollTo({ top: restoreScrollTarget })
      pendingRestoreRef.current = false
    }
  }, [windowed.state.lines.length, restoreScrollTarget, isTailing])

  useEffect(() => {
    if (isTailing) pendingRestoreRef.current = false
  }, [isTailing])

  // STAGE-004-024: historical query → buffer effect (NEW)
  const { reset: resetWindowed, prependOlder } = windowed
  useEffect(() => {
    if (frozen) {
      // If the key matches the seeded pinned key (the range settling after Stop),
      // leave frozen. If a genuine user query action changed the key, un-freeze.
      const queryKey = `${expr}|${startIso}|${endIso}|${servicesCsv}`
      if (queryKey !== prevQueryKeyRef.current) {
        setFrozen(false)
      }
      return
    }
    const queryKey = `${expr}|${startIso}|${endIso}|${servicesCsv}`
    const keyChanged = queryKey !== prevQueryKeyRef.current
    // Early-return guard: if neither the query key nor pages count changed since
    // the last dispatch, there is no genuine data change — skip to prevent a
    // dispatch-per-render when pages/flatLines refs churn without new data.
    if (!keyChanged && pages.length === prevPagesLenRef.current) {
      return
    }
    if (keyChanged || pages.length <= 1) {
      resetWindowed(flatLines)
      setLoadNewerError(null)
      prevQueryKeyRef.current = queryKey
      prevPagesLenRef.current = pages.length
      return
    }
    if (pages.length > prevPagesLenRef.current) {
      const lastPage = pages[pages.length - 1]
      if (lastPage !== undefined) prependOlder(lastPage.lines)
      prevPagesLenRef.current = pages.length
    }
  }, [
    frozen,
    expr,
    startIso,
    endIso,
    servicesCsv,
    pages,
    flatLines,
    resetWindowed,
    prependOlder,
    setLoadNewerError,
  ])

  // STAGE-004-024: sticky auto-scroll while tailing. Keyed on the live line count
  // so each new batch scrolls to bottom when sticky. The `!isTailing` gate on the
  // restore effect guarantees these two layout effects are mutually exclusive.
  useLayoutEffect(() => {
    if (!isTailing || !stickyRef.current) return
    const el = getScrollContainer()
    if (el !== null && typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollHeight })
  }, [windowed.state.lines.length, isTailing])

  const header = (
    <>
      {selectedIdentities.length > 0 && (
        <div data-testid="selected-services" className="flex flex-wrap items-center gap-2">
          {selectedIdentities.map((i) => (
            <span
              key={`${i.source_type}:${i.service}`}
              data-testid="service-chip"
              data-service={i.service}
              data-source-type={i.source_type}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-accent px-2 py-0.5 text-xs"
            >
              {i.source_type}:{i.service}
              <button
                type="button"
                aria-label={`Remove ${i.source_type}:${i.service}`}
                data-testid="service-chip-remove"
                onClick={() => onToggleIdentity(i)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Row B — search line */}
      <form
        className="flex min-w-0 flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          onSubmitSearch()
        }}
      >
        {advancedMode ? (
          <LogsQlEditor
            value={liveLogsQl}
            onChange={onLiveLogsQlChange}
            onSubmit={onSubmitSearch}
            placeholder="Enter LogsQL…"
            ariaLabel="LogsQL query"
            className="min-w-0 flex-1"
          />
        ) : (
          <input
            type="text"
            data-testid="logs-search-input"
            aria-label="Search logs"
            className="flex h-9 min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            placeholder="Search logs (plain text)…"
            value={livePlainText}
            onChange={(e) => {
              onLivePlainTextChange(e.target.value)
            }}
          />
        )}
        {(advancedMode
          ? liveLogsQl.length > 0 || committedLogsQl.length > 0
          : livePlainText.length > 0 || committedPlainText.length > 0) && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-8 w-8 p-0"
                data-testid="logs-search-clear"
                aria-label="Clear search"
                onClick={onClearSearch}
              >
                <X />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Clear search</TooltipContent>
          </Tooltip>
        )}
        <AdvancedToggle checked={advancedMode} onChange={onToggleAdvanced} />
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="submit"
              size="sm"
              className="h-8 w-8 p-0"
              data-testid="logs-search-submit"
              aria-label="Search"
            >
              <Search />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Search</TooltipContent>
        </Tooltip>
      </form>

      {/* Row C — button row */}
      <div className="flex flex-wrap items-center gap-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-8 w-8 p-0"
              onClick={handleRefresh}
              disabled={logs.isFetching || isTailing}
              data-testid="logs-refresh"
              aria-label="Refresh"
            >
              <RefreshCw className={cn(logs.isFetching && 'animate-spin')} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Refresh</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant={sidebarOpen ? 'secondary' : 'outline'}
              className="h-8 w-8 p-0"
              onClick={() => setSidebarOpen((o) => !o)}
              data-testid="logs-filter-toggle"
              aria-label="Filters"
              aria-pressed={sidebarOpen}
            >
              <Filter />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Filters</TooltipContent>
        </Tooltip>

        <div
          className={cn(isTailing && 'pointer-events-none opacity-50')}
          aria-disabled={isTailing}
        >
          <TimeRangeControl
            mode="full"
            value={range}
            onChange={onRangeChange}
            presets={ALL_PRESETS}
            utcChecked={timezone === 'utc'}
            onToggleUtc={toggleTimezone}
          />
        </div>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant={isTailing ? 'default' : 'outline'}
              className={cn(
                'h-8 w-8 p-0',
                isTailing && 'bg-emerald-600 hover:bg-emerald-700 text-white',
              )}
              onClick={() => {
                if (isTailing) {
                  // STOP: freeze on-screen lines, pin the custom end to the stop moment.
                  // Seed prevQueryKeyRef with the key the memo will produce once the
                  // range settles to the pinned custom window. resolveCustomWindow with
                  // both start and end present returns them unchanged (now-independent),
                  // so pinnedStartIso/pinnedEndIso are deterministic.
                  setIsTailing(false)
                  const stopMoment = new Date()
                  const startDate = parseIso(startIso) ?? new Date(stopMoment.getTime() - 3_600_000)
                  const pinnedStartIso = toIsoZ(startDate)
                  const pinnedEndIso = toIsoZ(stopMoment)
                  prevQueryKeyRef.current = `${expr}|${pinnedStartIso}|${pinnedEndIso}|${servicesCsv}`
                  setFrozen(true)
                  onRangeChange({ kind: 'custom', start: startDate, end: stopMoment })
                } else {
                  // START: trim front for headroom, go sticky, suppress historical restore.
                  const { trimFrontTo } = windowed
                  trimFrontTo(RENDER_CAP - HEADROOM)
                  setSticky(true)
                  stickyRef.current = true
                  pendingRestoreRef.current = false
                  setFrozen(false)
                  setIsTailing(true)
                }
              }}
              data-testid="logs-tail-toggle"
              aria-label="Live tail"
              aria-pressed={isTailing}
            >
              <Activity />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{isTailing ? 'Stop live tail' : 'Live tail'}</TooltipContent>
        </Tooltip>

        <WrapIconToggle checked={wrap} onChange={setWrap} />

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-8 w-8 p-0"
              data-testid="logs-save-query"
              aria-label="Save query"
              onClick={onOpenSave}
            >
              <Save />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Save query</TooltipContent>
        </Tooltip>

        <ExportButton expr={expr} startIso={startIso} endIso={endIso} servicesCsv={servicesCsv} />
      </div>

      {isGenericApiError && (
        <p role="alert" className="text-sm text-red-600">
          Failed to load logs: {logs.error?.message}
        </p>
      )}
      {(tail.status === 'error' && tail.error !== null) || loadNewerError !== null ? (
        <p role="alert" className="text-sm text-red-600" data-testid="tail-error">
          {tail.status === 'error' && tail.error !== null ? tail.error.message : loadNewerError}
        </p>
      ) : null}
    </>
  )

  // STAGE-004-024: handleLoadNewer callback
  const { appendNewer } = windowed
  const handleLoadNewer = useCallback(() => {
    if (isTailing || isLoadingNewer) return
    const arr = linesRef.current
    const last = arr.length > 0 ? arr[arr.length - 1] : undefined
    const newestShown = last !== undefined ? last.timestamp : startIso
    setIsLoadingNewer(true)
    setLoadNewerError(null)
    setFrozen(false)
    void fetchNewerLogs(expr, newestShown, endIso, servicesCsv)
      .then((batch) => {
        appendNewer(batch)
        setLoadNewerError(null)
      })
      .catch((err: unknown) => {
        setLoadNewerError(err instanceof Error ? err.message : 'Failed to load newer lines')
      })
      .finally(() => {
        setIsLoadingNewer(false)
      })
  }, [isTailing, isLoadingNewer, startIso, endIso, expr, servicesCsv, appendNewer])

  const useLogs = (): UseLogsResult => {
    if (isUnavailable) {
      return {
        lines: undefined,
        isLoading: false,
        isError: true,
        error: logs.error instanceof ApiError ? logs.error : undefined,
        logStatus: 'unavailable',
      }
    }
    if (isGenericApiError) {
      return {
        lines: undefined,
        isLoading: false,
        isError: false,
        error: undefined,
      }
    }
    const lines = windowed.state.lines
    const status: LogViewerStatus | undefined =
      !hasData && lines.length === 0 ? undefined : lines.length === 0 ? 'no_lines' : 'available'
    return {
      lines,
      isLoading: logs.isLoading && lines.length === 0,
      isError: false,
      error: undefined,
      logStatus: status,
      hasMore: logs.hasNextPage,
      isLoadingOlder: logs.isFetchingNextPage,
      loadOlder: () => {
        if (isTailing) setIsTailing(false)
        setFrozen(false)
        void logs.fetchNextPage()
      },
      trimmedOlder: windowed.state.trimmedOlder,
      trimmedNewer: windowed.state.trimmedNewer,
      hasNewer: !isTailing,
      isLoadingNewer,
      loadNewer: handleLoadNewer,
    }
  }

  // STAGE-004-016 add-to-filter adapters. service → add identity (additive only).
  // msg → append substring to the search.
  const handleAddServiceFilter = (service: string, sourceType: string): void => {
    onAddIdentity({ service, source_type: sourceType })
  }
  const handleAddMsgFilter = (value: string): void => {
    onAddMsgFilter(value)
  }
  const handleAddFieldFilter = (field: string, value: string): void => {
    onAddFieldFilter?.(field, value)
  }

  const inspector =
    selection !== null ? (
      <FieldInspectorPanel
        line={selection.line}
        onClose={() => setSelection(null)}
        onAddServiceFilter={handleAddServiceFilter}
        onAddMsgFilter={handleAddMsgFilter}
        onAddFieldFilter={handleAddFieldFilter}
      />
    ) : null

  const sidebar = (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div
        className="flex shrink-0 gap-1 overflow-x-auto"
        data-testid="logs-sidebar-tabs"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'services'}
          data-testid="logs-sidebar-tab-services"
          className={cn('rounded px-2 py-1 text-xs', sidebarTab === 'services' && 'bg-accent')}
          onClick={() => setSidebarTab('services')}
        >
          Services
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'saved'}
          data-testid="logs-sidebar-tab-saved"
          className={cn('rounded px-2 py-1 text-xs', sidebarTab === 'saved' && 'bg-accent')}
          onClick={() => setSidebarTab('saved')}
        >
          Saved
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'history'}
          data-testid="logs-sidebar-tab-history"
          className={cn('rounded px-2 py-1 text-xs', sidebarTab === 'history' && 'bg-accent')}
          onClick={() => setSidebarTab('history')}
        >
          Recent
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={sidebarTab === 'fields'}
          data-testid="logs-sidebar-tab-fields"
          className={cn('rounded px-2 py-1 text-xs', sidebarTab === 'fields' && 'bg-accent')}
          onClick={() => setSidebarTab('fields')}
        >
          Fields
        </button>
      </div>
      <div role="tabpanel" className="min-h-0 flex-1 overflow-y-auto">
        {sidebarTab === 'services' ? (
          <StreamPickerSidebar
            services={servicesData?.services ?? []}
            truncated={servicesData?.truncated ?? false}
            selectedIdentities={selectedIdentities}
            onToggleIdentity={onToggleIdentity}
            onSelectIdentities={onSelectIdentities}
            onDeselectIdentities={onDeselectIdentities}
            isLoading={servicesQuery.isLoading}
            isError={servicesQuery.isError}
          />
        ) : sidebarTab === 'saved' ? (
          <SavedQueriesPanel onLoad={onLoadSavedQuery} onUpdate={onUpdateSavedQuery} />
        ) : sidebarTab === 'history' ? (
          <QueryHistoryPanel onLoad={onLoadHistoryEntry} />
        ) : (
          <FieldsDiscoveryPanel
            expr={expr}
            start={startIso}
            end={endIso}
            services={servicesCsv}
            onAddFieldFilter={handleAddFieldFilter}
          />
        )}
      </div>
    </div>
  )

  return (
    <div className="flex h-full min-h-0 gap-4">
      {/* Desktop push-layout sidebar: rendered only when open. */}
      {!isMobile && sidebarOpen && (
        <aside className="flex h-full min-h-0 w-56 shrink-0 flex-col">{sidebar}</aside>
      )}

      <div className="relative flex h-full min-h-0 min-w-0 flex-1 flex-col">
        <div className="shrink-0 border-b border-border pb-1">
          <HistogramChart
            expr={expr}
            start={startIso}
            end={endIso}
            services={servicesCsv}
            onNarrowRange={onNarrowRange}
          />
        </div>

        <LogViewer
          useLogs={useLogs}
          headerSlot={header}
          emptyStateCopy={EMPTY_COPY}
          unavailableCopy={UNAVAILABLE_COPY}
          wrap={wrap}
          timezone={timezone}
          fieldInspectorEnabled
          fillHeight
          selectedKey={selection?.key ?? null}
          onLineSelected={(line, key) => {
            setSelection(line !== null && key !== null ? { key, line } : null)
          }}
        />

        {isTailing && !sticky && (
          <div className="pointer-events-none absolute inset-x-0 bottom-4 flex justify-center">
            <Button
              type="button"
              size="sm"
              variant="secondary"
              className="pointer-events-auto shadow-md"
              data-testid="tail-resume-autoscroll"
              onClick={() => {
                setSticky(true)
                stickyRef.current = true
                const el = getScrollContainer()
                if (el !== null) el.scrollTo({ top: el.scrollHeight })
              }}
            >
              Resume auto-scroll
            </Button>
          </div>
        )}
      </div>

      {/* STAGE-004-016: desktop inspector — right-side push aside. */}
      {!isMobile && inspector !== null && (
        <aside className="h-full min-h-0 w-80 shrink-0" data-testid="field-inspector-aside">
          {inspector}
        </aside>
      )}

      {/* Mobile: full-screen left drawer, headerless (sr-only title for a11y). */}
      {isMobile && (
        <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
          <SheetContent aria-describedby={undefined}>
            <SheetTitle className="sr-only">Filters</SheetTitle>
            {sidebar}
          </SheetContent>
        </Sheet>
      )}

      {/* STAGE-004-016: mobile inspector — right-side drawer. */}
      {isMobile && (
        <Sheet
          open={selection !== null}
          onOpenChange={(open) => {
            if (!open) setSelection(null)
          }}
        >
          <SheetContent side="right" aria-describedby={undefined}>
            <SheetTitle className="sr-only">Field inspector</SheetTitle>
            {inspector}
          </SheetContent>
        </Sheet>
      )}
    </div>
  )
}
