// STAGE-007-023 — Unifi Logs tab: the shared <LogViewer> over the UDM (gateway) syslog
// stream. Category preset chips + optional client-IP filter (src/dst) in the headerSlot.
import { useState, type JSX } from 'react'
import { RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { ALL_PRESETS } from '@/lib/timeRange'
import type { TimeRangeValue, PresetToken } from '@/lib/timeRange'
import { UDM_LOG_CATEGORIES, buildUdmLogsExpr, type UdmLogCategory } from './udmLogFilters'
import { useUdmLogViewer } from './useUdmLogViewer'

const EMPTY_COPY =
  'No matching UDM (gateway) log lines in the selected range. Try widening the time window, switching category, or clearing the IP filter.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

function explorerRangeFrom(range: TimeRangeValue): {
  sincePreset?: PresetToken
  rangeStart?: Date
  rangeEnd?: Date
} {
  if (range.kind === 'preset') return { sincePreset: range.token }
  return {
    ...(range.start !== undefined ? { rangeStart: range.start } : {}),
    ...(range.end !== undefined ? { rangeEnd: range.end } : {}),
  }
}

export function UnifiLogsTab(): JSX.Element {
  const [category, setCategory] = useState<UdmLogCategory>('all')
  // Committed IP filter (applied to the expr). Draft input is separate so typing doesn't
  // refetch on every keystroke; commit on Enter / blur.
  const [ipDraft, setIpDraft] = useState('')
  const [ip, setIp] = useState('')

  const expr = buildUdmLogsExpr(category, ip)
  const v = useUdmLogViewer(expr)

  const commitIp = (): void => setIp(ipDraft.trim())

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2" data-testid="unifi-logs-category-chips">
        {UDM_LOG_CATEGORIES.map((c) => (
          <Button
            key={c.value}
            size="sm"
            variant={category === c.value ? 'default' : 'outline'}
            onClick={() => setCategory(c.value)}
            data-testid={`unifi-logs-cat-${c.value}`}
            aria-pressed={category === c.value}
          >
            {c.label}
          </Button>
        ))}
        <Input
          value={ipDraft}
          onChange={(e) => setIpDraft(e.target.value)}
          onBlur={commitIp}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitIp()
          }}
          placeholder="Filter by client IP (src/dst)"
          aria-label="Filter by client IP"
          data-testid="unifi-logs-ip-input"
          className="h-8 w-56"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <OpenInExplorerButton logsQl={expr} {...explorerRangeFrom(v.range)} />
        <WrapToggle checked={v.wrap} onChange={v.setWrap} id="unifi-logs-wrap" />
        <TimezoneToggle
          checked={v.timezone === 'utc'}
          onChange={v.toggleTimezone}
          id="unifi-logs-tz-toggle"
        />
        <TimeRangeControl value={v.range} onChange={v.setRange} presets={ALL_PRESETS} />
        <Button
          size="sm"
          variant="outline"
          onClick={v.handleRefresh}
          disabled={v.isFetching}
          data-testid="unifi-logs-refresh"
        >
          <RefreshCw className="mr-1 size-4" />
          {v.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  )

  return (
    <div className="flex h-full min-h-0 flex-col p-4">
      <LogViewer
        fillHeight
        useLogs={v.useLogs}
        headerSlot={header}
        emptyStateCopy={EMPTY_COPY}
        unavailableCopy={UNAVAILABLE_COPY}
        wrap={v.wrap}
        timezone={v.timezone}
      />
    </div>
  )
}
