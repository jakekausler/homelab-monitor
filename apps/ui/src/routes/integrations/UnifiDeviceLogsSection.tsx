// STAGE-007-023 — Embedded UDM logs on the Unifi device page. HONEST: UDM syslog is
// GATEWAY-sourced. Infra devices (APs/switches) do NOT emit to this stream, and the device
// detail payload (UnifiDeviceDetail) carries NO IP, so we cannot filter traffic by this
// device's IP. We therefore show the gateway's "All" UDM log stream with an explicit note,
// rather than a fabricated per-device filter.
import { type JSX } from 'react'
import { RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { LogViewer } from '@/components/logs/LogViewer'
import { TimeRangeControl } from '@/components/logs/TimeRangeControl'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { ALL_PRESETS } from '@/lib/timeRange'
import { PanelSection } from './PanelSection'
import { buildUdmLogsExpr } from './udmLogFilters'
import { useUdmLogViewer } from './useUdmLogViewer'

const NOTE =
  "UDM syslog is gateway-sourced. These are the gateway's own logs — infra devices (APs/switches) do not emit here, and per-device IP filtering is unavailable for this device, so the full gateway stream is shown."
const EMPTY_COPY = 'No UDM gateway log lines in the selected range.'
const UNAVAILABLE_COPY = 'Logs backend (VictoriaLogs) is unavailable. Check service health.'

export function UnifiDeviceLogsSection(): JSX.Element {
  const expr = buildUdmLogsExpr('all')
  const v = useUdmLogViewer(expr)

  const header = (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-muted-foreground" data-testid="unifi-device-logs-note">
        {NOTE}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <OpenInExplorerButton logsQl={expr} sincePreset="1h" />
        <TimeRangeControl value={v.range} onChange={v.setRange} presets={ALL_PRESETS} />
        <Button
          size="sm"
          variant="outline"
          onClick={v.handleRefresh}
          disabled={v.isFetching}
          data-testid="unifi-device-logs-refresh"
        >
          <RefreshCw className="mr-1 size-4" />
          {v.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </div>
    </div>
  )

  return (
    <PanelSection title="Gateway logs">
      <LogViewer
        useLogs={v.useLogs}
        headerSlot={header}
        emptyStateCopy={EMPTY_COPY}
        unavailableCopy={UNAVAILABLE_COPY}
        timezone={v.timezone}
      />
    </PanelSection>
  )
}
