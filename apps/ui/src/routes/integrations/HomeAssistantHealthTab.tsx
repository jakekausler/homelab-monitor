// Placeholder body replaced by STAGE-005-023
import type { JSX } from 'react'

import { PanelSection } from './PanelSection'

export function HomeAssistantHealthTab(): JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
      <PanelSection title="Entity health">
        <p className="text-sm text-muted-foreground">Entity health will appear here.</p>
      </PanelSection>
      <PanelSection title="Battery">
        <p className="text-sm text-muted-foreground">Battery status will appear here.</p>
      </PanelSection>
    </div>
  )
}
