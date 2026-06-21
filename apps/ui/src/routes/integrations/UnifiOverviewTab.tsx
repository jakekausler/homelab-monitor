import type { JSX } from 'react'

import {
  useUnifiControllerHealth,
  useUnifiDevices,
  useUnifiDpi,
  useUnifiTeleport,
  useUnifiThreats,
} from '@/api/unifi'

import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'
import { UnifiControllerHealthWidget } from './UnifiControllerHealthWidget'
import { UnifiDeviceTable } from './UnifiDeviceTable'
import { UnifiDpiWidget } from './UnifiDpiWidget'
import { UnifiTeleportWidget } from './UnifiTeleportWidget'
import { UnifiThreatsWidget } from './UnifiThreatsWidget'

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
