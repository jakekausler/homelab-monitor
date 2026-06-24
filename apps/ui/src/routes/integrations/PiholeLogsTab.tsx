import type { JSX } from 'react'

import { PanelSection } from './PanelSection'

/**
 * STAGE-006-021 — placeholder shell for the Pi-hole Logs tab.
 *
 * STAGE-006-024 replaces this with the embedded LogViewer + live query feed.
 */
export function PiholeLogsTab(): JSX.Element {
  return (
    <div className="h-full space-y-4 overflow-y-auto p-4" data-testid="pihole-logs-tab">
      <PanelSection title="Query log">
        <p className="text-sm text-muted-foreground">Coming soon (STAGE-006-024)</p>
      </PanelSection>
    </div>
  )
}
