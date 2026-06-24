import type { JSX } from 'react'

import { PanelSection } from './PanelSection'

/**
 * STAGE-006-021 — placeholder shell for the Pi-hole Overview tab.
 *
 * Future widgets land here in later stages:
 *   - STAGE-006-022: Blocking control, Gravity & adlists, Messages
 *   - STAGE-006-023: Upstreams & Unbound, Clients, Recent blocked,
 *                    Version & container
 *
 * Each <PanelSection> below is an intentional placeholder; replace the
 * "Coming soon" note with the real widget when its owning stage is built.
 */
function ComingSoon({ stage }: { stage: string }): JSX.Element {
  return <p className="text-sm text-muted-foreground">Coming soon ({stage})</p>
}

export function PiholeOverviewTab(): JSX.Element {
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4" data-testid="pihole-overview-tab">
      {/* STAGE-006-022 */}
      <PanelSection title="Blocking control">
        <ComingSoon stage="STAGE-006-022" />
      </PanelSection>

      {/* STAGE-006-022 */}
      <PanelSection title="Gravity & adlists">
        <ComingSoon stage="STAGE-006-022" />
      </PanelSection>

      {/* STAGE-006-022 */}
      <PanelSection title="Messages">
        <ComingSoon stage="STAGE-006-022" />
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
