import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'

import { identitiesToServicesCsv, type ServiceIdentity } from '@/api/logs'
import {
  type SavedQuery,
  type SaveQueryCreateRequest,
  useUpdateSavedLogQuery,
} from '@/api/savedLogQueries'
import { recordQuery, type HistoryEntry } from '@/lib/queryHistory'
import {
  loadExplorerState,
  patchExplorerState,
  resolveInitialExplorerState,
  type ExplorerSeed,
} from '@/lib/explorerState'
import { fieldFilterClause, msgFilterClause, translateSearchToLogsQl } from '@/lib/logsQlTranslate'
import { LogsExplorerBody } from './LogsExplorerBody'
import { SaveQueryModal } from './SaveQueryModal'
import {
  CreateAlertModal,
  scaffoldLogsqlExpr,
  type CreateAlertFormValues,
} from '@/components/logs/CreateAlertModal'
import {
  ALL_PRESETS,
  parseIso,
  resolveCustomWindow,
  toIsoZ,
  type PresetToken,
  type TimeRangeValue,
} from '@/lib/timeRange'

const DEFAULT_PRESET: PresetToken = '1h'

function isPresetToken(s: string): s is PresetToken {
  return (ALL_PRESETS as readonly string[]).includes(s)
}

export function LogsExplorerPage() {
  const search = useSearch({ strict: false })
  const navigate = useNavigate()

  const q = typeof search.q === 'string' ? search.q : undefined
  const logsql = typeof search.logsql === 'string' ? search.logsql : undefined
  const since = typeof search.since === 'string' ? search.since : undefined
  const start = typeof search.start === 'string' ? search.start : undefined
  const end = typeof search.end === 'string' ? search.end : undefined

  // Read the `services` URL param (parsed as ServiceIdentity[], set by router validateSearch):
  const servicesParam: ServiceIdentity[] | undefined = Array.isArray(search.services)
    ? search.services
    : undefined

  // Compute the initial seed ONCE: URL precedence over persisted (TTL-checked).
  // Lazy initializer → evaluated a single time at mount.
  const [seed] = useState<ExplorerSeed>(() =>
    resolveInitialExplorerState(
      { q, logsql, since, start, end, services: servicesParam },
      loadExplorerState(),
    ),
  )

  // Three independent committed values + one live value per mode. Toggling modes
  // preserves BOTH texts; only the ACTIVE mode's committed value drives the
  // query and the URL. Seeded from the resolved seed.
  const [advancedMode, setAdvancedMode] = useState<boolean>(seed.advancedMode)
  const [committedPlainText, setCommittedPlainText] = useState<string>(seed.plainText)
  const [livePlainText, setLivePlainText] = useState<string>(seed.plainText)
  const [committedLogsQl, setCommittedLogsQl] = useState<string>(seed.logsQl)
  const [liveLogsQl, setLiveLogsQl] = useState<string>(seed.logsQl)
  const [range, setRange] = useState<TimeRangeValue>(seed.range)
  const [selectedIdentities, setSelectedIdentities] = useState<ServiceIdentity[]>(
    seed.selectedIdentities,
  )

  // Modal state for saving a query
  const [saveOpen, setSaveOpen] = useState(false)
  const [createAlertOpen, setCreateAlertOpen] = useState(false)

  const updateMut = useUpdateSavedLogQuery()

  // Build a HistoryEntry from the values writeUrl is about to commit (NOT from
  // state — state may not have updated yet when writeUrl runs). Mirrors
  // serializeCurrentExplorerState's range logic against the passed-in range.
  const buildHistoryEntry = (
    advanced: boolean,
    plain: string,
    lql: string,
    r: TimeRangeValue,
    ids: ServiceIdentity[],
  ): HistoryEntry => {
    const logsQl = advanced ? lql : plain
    const base = {
      id: crypto.randomUUID(),
      timestamp: Date.now(),
      logs_ql: logsQl,
      selected_services: ids.map((i) => ({ service: i.service, source_type: i.source_type })),
      advanced_mode: advanced,
    }
    if (r.kind === 'preset') {
      return { ...base, since_preset: r.token }
    }
    const now = new Date()
    const win = resolveCustomWindow({ start: r.start, end: r.end }, { now, maxSpanDays: 30 })
    return { ...base, range_start_iso: toIsoZ(win.start), range_end_iso: toIsoZ(win.end) }
  }

  // Build the URL search object by OMITTING absent keys (exactOptionalPropertyTypes:
  // never write `key: undefined`). Advanced → write `logsql`, omit `q`. Plain →
  // write `q`, omit `logsql`. Empty active text → omit that key entirely.
  const writeUrl = (
    advanced: boolean,
    plain: string,
    lql: string,
    r: TimeRangeValue,
    ids: ServiceIdentity[],
  ): void => {
    const next: {
      q?: string
      logsql?: string
      since?: string
      start?: string
      end?: string
      services?: string
    } = {}
    if (advanced) {
      if (lql.length > 0) next.logsql = lql
    } else {
      if (plain.length > 0) next.q = plain
    }
    if (r.kind === 'preset') {
      next.since = r.token
    } else {
      if (r.start !== undefined) next.start = toIsoZ(r.start)
      if (r.end !== undefined) next.end = toIsoZ(r.end)
    }
    // `services` is serialized as the locked CSV `<source_type>:<service>` URL format
    // (D-012A-URL); parseServicesParam (router.tsx) parses it back to ServiceIdentity[]
    // on read. The cast at the navigate call below bridges this CSV-string write value
    // to validateSearch's ServiceIdentity[] return type.
    if (ids.length > 0) next.services = identitiesToServicesCsv(ids)
    void navigate({
      to: '/logs/query',
      search: next as unknown as { services?: ServiceIdentity[] },
    })
    recordQuery(buildHistoryEntry(advanced, plain, lql, r, ids))
  }

  const handleSubmitSearch = (): void => {
    if (advancedMode) {
      setCommittedLogsQl(liveLogsQl)
      writeUrl(true, committedPlainText, liveLogsQl, range, selectedIdentities)
    } else {
      setCommittedPlainText(livePlainText)
      writeUrl(false, livePlainText, committedLogsQl, range, selectedIdentities)
    }
  }

  const handleClearSearch = (): void => {
    // Clear ONLY the active mode's text (live + committed); the other mode is
    // preserved.
    if (advancedMode) {
      setLiveLogsQl('')
      setCommittedLogsQl('')
      writeUrl(true, committedPlainText, '', range, selectedIdentities)
    } else {
      setLivePlainText('')
      setCommittedPlainText('')
      writeUrl(false, '', committedLogsQl, range, selectedIdentities)
    }
  }

  // STAGE-004-016 fix — append a discrete _msg:"…" clause to the LogsQL query.
  // Each add-to-filter call produces its own independent substring constraint,
  // ANDed with any existing query (LogsQL space-separates phrases as AND).
  // Switches to advanced mode so clauses are composed as real LogsQL, not as a
  // single contiguous phrase. If a plain-text search is active, it is first
  // translated into a _msg:"…" clause before appending the new one.
  // Routes through writeUrl → STAGE-015 persistence + history both fire.
  const appendMsgFilter = (value: string): void => {
    const clause = msgFilterClause(value)
    if (clause === null) return

    // Determine the current LogsQL base to append to.
    let base: string
    if (advancedMode) {
      // Already in advanced mode: use committed LogsQL verbatim.
      base = committedLogsQl.trim()
    } else {
      // Plain mode: translate current plain text to a _msg clause (or '' if empty).
      const plainClause =
        committedPlainText.trim().length > 0 ? translateSearchToLogsQl(committedPlainText) : ''
      base = plainClause
    }

    const nextLogsQl = base.length > 0 ? `${base} ${clause}` : clause

    setAdvancedMode(true)
    setCommittedLogsQl(nextLogsQl)
    setLiveLogsQl(nextLogsQl)
    writeUrl(true, committedPlainText, nextLogsQl, range, selectedIdentities)
  }

  // STAGE-004-016A — append a structured field:"value" clause to the LogsQL query.
  // Mirrors appendMsgFilter but uses fieldFilterClause(field, value) instead of
  // msgFilterClause(value). Same advanced-mode switch + plain-text translation +
  // writeUrl routing.
  const appendFieldFilter = (field: string, value: string): void => {
    const clause = fieldFilterClause(field, value)
    if (clause === null) return

    let base: string
    if (advancedMode) {
      base = committedLogsQl.trim()
    } else {
      const plainClause =
        committedPlainText.trim().length > 0 ? translateSearchToLogsQl(committedPlainText) : ''
      base = plainClause
    }

    const nextLogsQl = base.length > 0 ? `${base} ${clause}` : clause

    setAdvancedMode(true)
    setCommittedLogsQl(nextLogsQl)
    setLiveLogsQl(nextLogsQl)
    writeUrl(true, committedPlainText, nextLogsQl, range, selectedIdentities)
  }

  const handleRangeChange = (next: TimeRangeValue): void => {
    setRange(next)
    writeUrl(advancedMode, committedPlainText, committedLogsQl, next, selectedIdentities)
  }

  // STAGE-004-019 — narrow the range to a clicked histogram bucket
  // [startIso, endIso). Commits a CUSTOM absolute range via the same setRange +
  // writeUrl choke-point as the range picker, so URL + persistence + history all
  // fire. Bad ISO (shouldn't happen) is a no-op.
  const handleNarrowRange = (startIso: string, endIso: string): void => {
    const s = parseIso(startIso)
    const e = parseIso(endIso)
    if (s === null || e === null) return
    const next: TimeRangeValue = { kind: 'custom', start: s, end: e }
    setRange(next)
    writeUrl(advancedMode, committedPlainText, committedLogsQl, next, selectedIdentities)
  }

  const handleToggleAdvanced = (nextAdvanced: boolean): void => {
    setAdvancedMode(nextAdvanced)
    // Rewrite the URL to reflect the NEW active mode's COMMITTED value. Both
    // texts are preserved in state across the toggle.
    writeUrl(nextAdvanced, committedPlainText, committedLogsQl, range, selectedIdentities)
  }

  const sameIdentity = (a: ServiceIdentity, b: ServiceIdentity): boolean =>
    a.service === b.service && a.source_type === b.source_type

  const handleToggleIdentity = (identity: ServiceIdentity): void => {
    setSelectedIdentities((prev) => {
      const exists = prev.some((i) => sameIdentity(i, identity))
      const next = exists ? prev.filter((i) => !sameIdentity(i, identity)) : [...prev, identity]
      writeUrl(advancedMode, committedPlainText, committedLogsQl, range, next)
      return next
    })
  }

  // STAGE-004-016 fix: additive-only identity add (used by the inspector + button).
  // No-ops if the identity is already selected. Keeps the chip × (via
  // onToggleIdentity) as the only removal path.
  const handleAddIdentity = (identity: ServiceIdentity): void => {
    setSelectedIdentities((prev) => {
      if (prev.some((i) => sameIdentity(i, identity))) return prev
      const next = [...prev, identity]
      writeUrl(advancedMode, committedPlainText, committedLogsQl, range, next)
      return next
    })
  }

  const handleSelectIdentities = (identities: ServiceIdentity[]): void => {
    setSelectedIdentities((prev) => {
      const next = [...prev]
      for (const id of identities) {
        if (!next.some((i) => sameIdentity(i, id))) next.push(id)
      }
      writeUrl(advancedMode, committedPlainText, committedLogsQl, range, next)
      return next
    })
  }

  const handleDeselectIdentities = (identities: ServiceIdentity[]): void => {
    setSelectedIdentities((prev) => {
      const next = prev.filter((i) => !identities.some((id) => sameIdentity(id, i)))
      writeUrl(advancedMode, committedPlainText, committedLogsQl, range, next)
      return next
    })
  }

  // The name-less common payload shared by buildSavePayload and history recording.
  // Mirrors the existing buildSavePayload range logic EXACTLY.
  const serializeCurrentExplorerState = useCallback((): Omit<SaveQueryCreateRequest, 'name'> => {
    const logsQl = advancedMode ? committedLogsQl : committedPlainText
    const base = {
      logs_ql: logsQl,
      selected_services: selectedIdentities.map((i) => ({
        service: i.service,
        source_type: i.source_type,
      })),
      advanced_mode: advancedMode,
    }
    if (range.kind === 'preset') {
      return { ...base, since_preset: range.token }
    }
    const now = new Date()
    const win = resolveCustomWindow(
      { start: range.start, end: range.end },
      { now, maxSpanDays: 30 },
    )
    return { ...base, range_start_iso: toIsoZ(win.start), range_end_iso: toIsoZ(win.end) }
  }, [advancedMode, committedLogsQl, committedPlainText, selectedIdentities, range])

  // Persist the committed query state on every change (executed query, mode
  // toggle, range change, services change). Mirrors serializeCurrentExplorerState's
  // range branching. Does NOT write scroll_position — the Body owns that; the
  // read-modify-write merge in patchExplorerState preserves it. Fires once on
  // mount too (re-saving the seeded values, harmless).
  useEffect(() => {
    const state = serializeCurrentExplorerState()
    patchExplorerState(state)
  }, [serializeCurrentExplorerState])

  const buildSavePayload = (name: string): SaveQueryCreateRequest => {
    return { name, ...serializeCurrentExplorerState() }
  }

  const handleUpdateSavedQuery = (query: SavedQuery): void => {
    // Overwrite the saved row's payload with the CURRENT Explorer state.
    // buildSavePayload uses the current committed mode/text/range/identities.
    // name is carried for schema completeness but the backend ignores it on PUT.
    const body = buildSavePayload(query.name)
    updateMut.mutate({ id: query.id, body })
  }

  // The field subset both SavedQuery and HistoryEntry satisfy for load/reconstruct.
  type LoadablePayload = Pick<
    SaveQueryCreateRequest,
    | 'advanced_mode'
    | 'logs_ql'
    | 'selected_services'
    | 'since_preset'
    | 'range_start_iso'
    | 'range_end_iso'
  >

  const reconstructFromPayload = (p: LoadablePayload): void => {
    const nextAdvanced = p.advanced_mode
    const nextPlain = nextAdvanced ? '' : p.logs_ql
    const nextLogsQl = nextAdvanced ? p.logs_ql : ''

    let nextRange: TimeRangeValue
    if (p.since_preset != null && isPresetToken(p.since_preset)) {
      nextRange = { kind: 'preset', token: p.since_preset }
    } else if (p.range_start_iso != null && p.range_end_iso != null) {
      const s = parseIso(p.range_start_iso)
      const e = parseIso(p.range_end_iso)
      nextRange = {
        kind: 'custom',
        ...(s !== null ? { start: s } : {}),
        ...(e !== null ? { end: e } : {}),
      }
    } else {
      nextRange = { kind: 'preset', token: DEFAULT_PRESET }
    }

    const nextIds: ServiceIdentity[] = (p.selected_services ?? []).map((s) => ({
      service: s.service,
      source_type: s.source_type,
    }))

    setAdvancedMode(nextAdvanced)
    setCommittedPlainText(nextPlain)
    setLivePlainText(nextPlain)
    setCommittedLogsQl(nextLogsQl)
    setLiveLogsQl(nextLogsQl)
    setRange(nextRange)
    setSelectedIdentities(nextIds)
    writeUrl(nextAdvanced, nextPlain, nextLogsQl, nextRange, nextIds)
  }

  const handleLoadSavedQuery = (saved: SavedQuery): void => {
    reconstructFromPayload(saved)
  }

  const handleLoadHistoryEntry = (entry: HistoryEntry): void => {
    reconstructFromPayload(entry)
  }

  // STAGE-043B — launch the alert modal in Simple or Advanced mode based on the
  // Explorer's current mode. Labels (rule_name/summary) are kept clean — the raw
  // query is NEVER echoed into them (messy text contaminated fields previously).
  const createAlertLaunch = useMemo<{
    initialMode: 'simple' | 'advanced'
    initialValues: Partial<CreateAlertFormValues>
  }>(() => {
    if (advancedMode) {
      // Advanced/LogsQL Explorer mode: the committed query is already valid LogsQL.
      // Append the count-threshold suffix only if there's no | stats pipe (safe —
      // no escaping needed; the Explorer query is already valid LogsQL).
      return {
        initialMode: 'advanced',
        initialValues: {
          expr: scaffoldLogsqlExpr(committedLogsQl),
          expr_kind: 'logsql',
        },
      }
    }
    // Plain-text Explorer mode: pre-fill Simple `contains` from the raw committed
    // text (the Simple builder escapes it). Defaults for threshold/window/severity.
    return {
      initialMode: 'simple',
      initialValues: {
        simple_contains: committedPlainText,
        expr_kind: 'logsql',
      },
    }
  }, [advancedMode, committedLogsQl, committedPlainText])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <LogsExplorerBody
        advancedMode={advancedMode}
        committedPlainText={committedPlainText}
        livePlainText={livePlainText}
        committedLogsQl={committedLogsQl}
        liveLogsQl={liveLogsQl}
        range={range}
        selectedIdentities={selectedIdentities}
        onLivePlainTextChange={setLivePlainText}
        onLiveLogsQlChange={setLiveLogsQl}
        onToggleAdvanced={handleToggleAdvanced}
        onSubmitSearch={handleSubmitSearch}
        onClearSearch={handleClearSearch}
        onRangeChange={handleRangeChange}
        onToggleIdentity={handleToggleIdentity}
        onAddIdentity={handleAddIdentity}
        onSelectIdentities={handleSelectIdentities}
        onDeselectIdentities={handleDeselectIdentities}
        onOpenSave={() => setSaveOpen(true)}
        onOpenCreateAlert={() => setCreateAlertOpen(true)}
        onLoadSavedQuery={handleLoadSavedQuery}
        onUpdateSavedQuery={handleUpdateSavedQuery}
        onLoadHistoryEntry={handleLoadHistoryEntry}
        restoreScrollTarget={seed.restoreScrollTarget}
        onAddMsgFilter={appendMsgFilter}
        onAddFieldFilter={appendFieldFilter}
        onNarrowRange={handleNarrowRange}
      />
      <SaveQueryModal open={saveOpen} onOpenChange={setSaveOpen} buildPayload={buildSavePayload} />
      <CreateAlertModal
        open={createAlertOpen}
        onOpenChange={setCreateAlertOpen}
        initialMode={createAlertLaunch.initialMode}
        initialValues={createAlertLaunch.initialValues}
        sourceKind="query"
      />
    </div>
  )
}
