import type { JSX } from 'react'

import { usePiholeOverview } from '@/api/pihole'

import { PanelSection } from './PanelSection'
import { PiholeBlockingWidget } from './PiholeBlockingWidget'
import { PiholeGravityWidget } from './PiholeGravityWidget'
import { PiholeMessagesWidget } from './PiholeMessagesWidget'

/**
 * STAGE-006-021 — Pi-hole Overview tab shell.
 * STAGE-006-022 — Blocking control, Gravity & adlists, Messages widgets + privacy banner.
 *
 * Remaining placeholders are owned by STAGE-006-023:
 *   Upstreams & Unbound, Clients, Recent blocked, Version & container.
 */
function ComingSoon({ stage }: { stage: string }): JSX.Element {
  return <p className="text-sm text-muted-foreground">Coming soon ({stage})</p>
}

export function PiholeOverviewTab(): JSX.Element {
  const overview = usePiholeOverview()
  const privacyLevel = overview.data?.privacy_level
  const privacyRestricted = privacyLevel != null && privacyLevel > 0

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4" data-testid="pihole-overview-tab">
      {privacyRestricted && (
        <div
          className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800"
          role="status"
          aria-live="polite"
          data-testid="pihole-privacy-banner"
        >
          Query logging restricted — data may be incomplete
        </div>
      )}

      {/* STAGE-006-022 */}
      <PanelSection title="Blocking control">
        <PiholeBlockingWidget />
      </PanelSection>

      {/* STAGE-006-022 */}
      <PanelSection title="Gravity & adlists">
        <PiholeGravityWidget />
      </PanelSection>

      {/* STAGE-006-022 */}
      <PanelSection title="Messages">
        <PiholeMessagesWidget />
      </PanelSection>

      {/* STAGE-006-023 */}
      <PanelSection title="Upstreams & Unbound">
        <ComingSoon stage="STAGE-006-023" />
      </PanelSection>

      {/* STAGE-006-023 */}
      <PanelSection title="Clients">
        <ComingSoon stage="STAGE-006-023" />
      </PanelSection>

      {/* STAGE-006-023 */}
      <PanelSection title="Recent blocked">
        <ComingSoon stage="STAGE-006-023" />
      </PanelSection>

      {/* STAGE-006-023 */}
      <PanelSection title="Version & container">
        <ComingSoon stage="STAGE-006-023" />
      </PanelSection>
    </div>
  )
}
