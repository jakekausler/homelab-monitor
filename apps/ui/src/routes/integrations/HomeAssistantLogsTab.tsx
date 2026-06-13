// Placeholder body replaced by STAGE-005-025
import type { JSX } from 'react'

import { PanelSection } from './PanelSection'

export function HomeAssistantLogsTab(): JSX.Element {
  return (
    <div className="p-4">
      <PanelSection title="Logs">
        <p className="text-sm text-muted-foreground">
          Recent Home Assistant logs will appear here.
        </p>
      </PanelSection>
    </div>
  )
}
