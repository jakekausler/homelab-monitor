import { useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'

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
  const since = typeof search.since === 'string' ? search.since : undefined
  const start = typeof search.start === 'string' ? search.start : undefined
  const end = typeof search.end === 'string' ? search.end : undefined

  // Committed state is seeded once from the URL. After mount, the URL is written
  // by our own handlers, and we keep committed state in sync there.
  const [committedSearchText, setCommittedSearchText] = useState<string>(q ?? '')
  const [liveSearchText, setLiveSearchText] = useState<string>(q ?? '')
  const [range, setRange] = useState<TimeRangeValue>(() => initialRange(since, start, end))

  // Build the URL search object by OMITTING absent keys (exactOptionalPropertyTypes:
  // never write `key: undefined`). `text` empty → omit `q`.
  const writeUrl = (text: string, r: TimeRangeValue): void => {
    const next: { q?: string; since?: string; start?: string; end?: string } = {}
    if (text.length > 0) next.q = text
    if (r.kind === 'preset') {
      next.since = r.token
    } else {
      if (r.start !== undefined) next.start = toIsoZ(r.start)
      if (r.end !== undefined) next.end = toIsoZ(r.end)
    }
    void navigate({ to: '/logs', search: next })
  }

  const handleSubmitSearch = (): void => {
    setCommittedSearchText(liveSearchText)
    writeUrl(liveSearchText, range)
  }

  const handleClearSearch = (): void => {
    setLiveSearchText('')
    setCommittedSearchText('')
    writeUrl('', range)
  }

  const handleRangeChange = (next: TimeRangeValue): void => {
    setRange(next)
    writeUrl(committedSearchText, next)
  }

  return (
    <div className="space-y-4">
      <LogsExplorerBody
        committedSearchText={committedSearchText}
        liveSearchText={liveSearchText}
        range={range}
        onLiveSearchTextChange={setLiveSearchText}
        onSubmitSearch={handleSubmitSearch}
        onClearSearch={handleClearSearch}
        onRangeChange={handleRangeChange}
      />
    </div>
  )
}
