import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'

import { useHomeAssistantSummary } from '@/api/home_assistant'

import { HaBatteryWidget } from './HaBatteryWidget'
import { HaEntityHealthWidget } from './HaEntityHealthWidget'
import { PanelSection } from './PanelSection'

export function HomeAssistantHealthTab(): JSX.Element {
  const result = useHomeAssistantSummary()

  return (
    <div className="space-y-4 p-4">
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Home Assistant metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {result.data?.ha_up === false && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Home Assistant offline (last seen: {result.data.last_seen ?? 'unknown'})
        </div>
      )}
      {result.data && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <PanelSection title="Entity health">
            <HaEntityHealthWidget entities={result.data.entities} />
          </PanelSection>
          <PanelSection title="Battery">
            <HaBatteryWidget battery={result.data.battery} />
          </PanelSection>
        </div>
      )}
    </div>
  )
}
