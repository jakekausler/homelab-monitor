import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'

import { useHomeAssistantSummary } from '@/api/home_assistant'

import { HaConfigEntriesDrill } from './HaConfigEntriesDrill'
import { HaIntegrationStatusWidget } from './HaIntegrationStatusWidget'
import { HaRepairsDrill } from './HaRepairsDrill'
import { HaUpdatesDrill } from './HaUpdatesDrill'
import { HaUpdatesWidget } from './HaUpdatesWidget'
import { PanelSection } from './PanelSection'

export function HomeAssistantStatusTab(): JSX.Element {
  const result = useHomeAssistantSummary()

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
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
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <PanelSection title="Updates">
              <HaUpdatesWidget updates={result.data.updates} />
            </PanelSection>
            <PanelSection title="Integration status">
              <HaIntegrationStatusWidget
                configEntries={result.data.config_entries}
                repairs={result.data.repairs}
                notifications={result.data.notifications}
              />
            </PanelSection>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <PanelSection title="Pending updates">
              <HaUpdatesDrill />
            </PanelSection>
            <PanelSection title="Integration errors">
              <HaConfigEntriesDrill />
            </PanelSection>
            <PanelSection title="Active repairs">
              <HaRepairsDrill />
            </PanelSection>
          </div>
        </>
      )}
    </div>
  )
}
