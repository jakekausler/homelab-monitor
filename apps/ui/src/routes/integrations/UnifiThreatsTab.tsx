// STAGE-007-023 — Unifi Threats tab: <LogViewer> pinned to UDM security-category audit lines
// (forensics only). The honest banner is ALWAYS visible: IPS/IDS may be disabled, and the live
// alert fires off the structured alarm path — NOT these syslog lines.
import { type JSX } from 'react'
import { RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { TimezoneToggle } from '@/components/logs/TimezoneToggle'
import { WrapToggle } from '@/components/logs/WrapToggle'
import { ALL_PRESETS } from '@/lib/timeRange'
import { buildUdmThreatsExpr } from './udmLogFilters'
import { useUdmLogViewer } from './useUdmLogViewer'

const EMPTY_COPY =
  'No security-relevant UDM events in the selected range. This does NOT confirm safety — see the banner above.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

const THREATS_BANNER =
  "Security-relevant UDM events: admin/audit actions and firewall blocks. IDS/IPS threat detection is delivered via the controller's structured alarm path — not this syslog stream — and requires CyberSecure/IPS to be enabled on the controller. An empty list is NOT proof of safety."

export function UnifiThreatsTab(): JSX.Element {
  const expr = buildUdmThreatsExpr()
  const v = useUdmLogViewer(expr)

  const header = (
    <div className="flex flex-col gap-3">
      <div
        role="status"
        data-testid="unifi-threats-banner"
        className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300"
      >
        {THREATS_BANNER}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="font-medium">UDM security events</span>
        <div className="flex flex-wrap items-center gap-2">
          <WrapToggle checked={v.wrap} onChange={v.setWrap} id="unifi-threats-wrap" />
          <TimezoneToggle
            checked={v.timezone === 'utc'}
            onChange={v.toggleTimezone}
            id="unifi-threats-tz-toggle"
          />
          <TimeRangeControl value={v.range} onChange={v.setRange} presets={ALL_PRESETS} />
          <Button
            size="sm"
            variant="outline"
            onClick={v.handleRefresh}
            disabled={v.isFetching}
            data-testid="unifi-threats-refresh"
          >
            <RefreshCw className="mr-1 size-4" />
            {v.isFetching ? 'Refreshing…' : 'Refresh'}
          </Button>
        </div>
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
