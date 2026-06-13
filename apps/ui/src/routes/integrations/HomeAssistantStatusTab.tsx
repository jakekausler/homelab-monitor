// Placeholder body replaced by STAGE-005-024
import type { JSX } from 'react'

import { PanelSection } from './PanelSection'

export function HomeAssistantStatusTab(): JSX.Element {
  return (
    <div className="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2">
      <PanelSection title="Updates">
        <p className="text-sm text-muted-foreground">Available updates will appear here.</p>
      </PanelSection>
      <PanelSection title="Integration status">
        <p className="text-sm text-muted-foreground">Integration status will appear here.</p>
      </PanelSection>
    </div>
  )
}
