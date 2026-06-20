import type { JSX, ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import {
  useUnifiControllerHealth,
  useUnifiDevices,
  useUnifiDpi,
  useUnifiTeleport,
  useUnifiThreats,
} from '@/api/unifi'

import { PanelSection } from './PanelSection'
import { UnifiControllerHealthWidget } from './UnifiControllerHealthWidget'
import { UnifiDeviceTable } from './UnifiDeviceTable'
import { UnifiDpiWidget } from './UnifiDpiWidget'
import { UnifiTeleportWidget } from './UnifiTeleportWidget'
import { UnifiThreatsWidget } from './UnifiThreatsWidget'

// Renders the standard pending / 502 / error states; renderData runs on success.
function QueryState<T>({
  result,
  unavailableLabel,
  renderData,
}: {
  result: UseQueryResult<T, ApiError>
  unavailableLabel: string
  renderData: (data: T) => ReactNode
}): JSX.Element {
  return (
    <>
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          {unavailableLabel}
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {result.data !== undefined && renderData(result.data)}
    </>
  )
}

export function UnifiOverviewTab(): JSX.Element {
  const devices = useUnifiDevices()
  const threats = useUnifiThreats()
  const dpi = useUnifiDpi()
  const teleport = useUnifiTeleport()
  const controllerHealth = useUnifiControllerHealth()

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <PanelSection title="Devices">
        <QueryState
          result={devices}
          unavailableLabel="Unifi devices temporarily unavailable"
          renderData={(data) => <UnifiDeviceTable devices={data.devices} />}
        />
      </PanelSection>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="Threats">
          <QueryState
            result={threats}
            unavailableLabel="Threat data temporarily unavailable"
            renderData={(data) => <UnifiThreatsWidget threats={data.threats} />}
          />
        </PanelSection>

        <PanelSection title="DPI top apps">
          <QueryState
            result={dpi}
            unavailableLabel="DPI data temporarily unavailable"
            renderData={(data) => <UnifiDpiWidget apps={data.apps} />}
          />
        </PanelSection>

        <PanelSection title="Teleport">
          <QueryState
            result={teleport}
            unavailableLabel="Teleport data temporarily unavailable"
            renderData={(data) => <UnifiTeleportWidget teleport={data} />}
          />
        </PanelSection>

        <PanelSection title="Controller health">
          <QueryState
            result={controllerHealth}
            unavailableLabel="Controller health temporarily unavailable"
            renderData={(data) => <UnifiControllerHealthWidget health={data} />}
          />
        </PanelSection>
      </div>
    </div>
  )
}
