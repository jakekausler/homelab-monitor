import { useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'

import { identitiesToServicesCsv, type ServiceIdentity } from '@/api/logs'
import {
  type SavedQuery,
  type SaveQueryCreateRequest,
  useUpdateSavedLogQuery,
} from '@/api/savedLogQueries'
import { LogsExplorerBody } from './LogsExplorerBody'
import { SaveQueryModal } from './SaveQueryModal'
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

/** Derive the initial committed TimeRangeValue from URL bounds. */
function initialRange(
  since: string | undefined,
  start: string | undefined,
  end: string | undefined,
): TimeRangeValue {
  const customStart = start !== undefined ? parseIso(start) : null
  const customEnd = end !== undefined ? parseIso(end) : null
  if (customStart !== null || customEnd !== null) {
    return {
      kind: 'custom',
      start: customStart ?? undefined,
      end: customEnd ?? undefined,
    }
  }
  if (since !== undefined && isPresetToken(since)) {
    return { kind: 'preset', token: since }
  }
  return { kind: 'preset', token: DEFAULT_PRESET }
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

  // Three independent committed values + one live value per mode. Toggling modes
  // preserves BOTH texts; only the ACTIVE mode's committed value drives the
  // query and the URL. Seeded once from the URL: `logsql` present → advanced.
  const [advancedMode, setAdvancedMode] = useState<boolean>(logsql !== undefined)
  const [committedPlainText, setCommittedPlainText] = useState<string>(q ?? '')
  const [livePlainText, setLivePlainText] = useState<string>(q ?? '')
  const [committedLogsQl, setCommittedLogsQl] = useState<string>(logsql ?? '')
  const [liveLogsQl, setLiveLogsQl] = useState<string>(logsql ?? '')
  const [range, setRange] = useState<TimeRangeValue>(() => initialRange(since, start, end))
  const [selectedIdentities, setSelectedIdentities] = useState<ServiceIdentity[]>(
    servicesParam ?? [],
  )

  // Modal state for saving a query
  const [saveOpen, setSaveOpen] = useState(false)

  const updateMut = useUpdateSavedLogQuery()

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
    void navigate({ to: '/logs', search: next as unknown as { services?: ServiceIdentity[] } })
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

  const handleRangeChange = (next: TimeRangeValue): void => {
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

  const buildSavePayload = (name: string): SaveQueryCreateRequest => {
    // logs_ql carries the active-mode committed expression. In advanced mode this
    // is the raw LogsQL; in plain mode we save the plain text verbatim into logs_ql
    // and rely on advanced_mode=false to tell load() to put it back in the plain box.
    const logsQl = advancedMode ? committedLogsQl : committedPlainText
    const base = {
      name,
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
    // custom: resolve open bounds to concrete ISO so the saved query is reproducible.
    // Use the SAME resolution the body uses (resolveCustomWindow) against a fresh now.
    const now = new Date()
    const win = resolveCustomWindow(
      { start: range.start, end: range.end },
      { now, maxSpanDays: 30 },
    )
    return { ...base, range_start_iso: toIsoZ(win.start), range_end_iso: toIsoZ(win.end) }
  }

  const handleUpdateSavedQuery = (query: SavedQuery): void => {
    // Overwrite the saved row's payload with the CURRENT Explorer state.
    // buildSavePayload uses the current committed mode/text/range/identities.
    // name is carried for schema completeness but the backend ignores it on PUT.
    const body = buildSavePayload(query.name)
    updateMut.mutate({ id: query.id, body })
  }

  const handleLoadSavedQuery = (saved: SavedQuery): void => {
    const nextAdvanced = saved.advanced_mode
    // A saved query carries ONE expression (in logs_ql, belonging to its active
    // mode). Clear the OTHER mode's buffer so toggling modes after load doesn't
    // surface the PREVIOUS query's stale text.
    const nextPlain = nextAdvanced ? '' : saved.logs_ql
    const nextLogsQl = nextAdvanced ? saved.logs_ql : ''

    // Reconstruct range: preset OR custom (from ISO strings).
    let nextRange: TimeRangeValue
    if (saved.since_preset != null && isPresetToken(saved.since_preset)) {
      nextRange = { kind: 'preset', token: saved.since_preset }
    } else if (saved.range_start_iso != null && saved.range_end_iso != null) {
      const s = parseIso(saved.range_start_iso)
      const e = parseIso(saved.range_end_iso)
      nextRange = {
        kind: 'custom',
        ...(s !== null ? { start: s } : {}),
        ...(e !== null ? { end: e } : {}),
      }
    } else {
      nextRange = { kind: 'preset', token: DEFAULT_PRESET }
    }

    const nextIds: ServiceIdentity[] = saved.selected_services.map((s) => ({
      service: s.service,
      source_type: s.source_type,
    }))

    // Force mode + commit all state, then write the URL (deep-linkable) using the
    // SAME writeUrl path the manual handlers use.
    setAdvancedMode(nextAdvanced)
    setCommittedPlainText(nextPlain)
    setLivePlainText(nextPlain)
    setCommittedLogsQl(nextLogsQl)
    setLiveLogsQl(nextLogsQl)
    setRange(nextRange)
    setSelectedIdentities(nextIds)
    writeUrl(nextAdvanced, nextPlain, nextLogsQl, nextRange, nextIds)
  }

  return (
    <div className="space-y-4">
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
        onSelectIdentities={handleSelectIdentities}
        onDeselectIdentities={handleDeselectIdentities}
        onOpenSave={() => setSaveOpen(true)}
        onLoadSavedQuery={handleLoadSavedQuery}
        onUpdateSavedQuery={handleUpdateSavedQuery}
      />
      <SaveQueryModal open={saveOpen} onOpenChange={setSaveOpen} buildPayload={buildSavePayload} />
    </div>
  )
}
