import { useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'

import { identitiesToServicesCsv, type ServiceIdentity } from '@/api/logs'
import { LogsExplorerBody } from './LogsExplorerBody'
import {
  ALL_PRESETS,
  parseIso,
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
      />
    </div>
  )
}
