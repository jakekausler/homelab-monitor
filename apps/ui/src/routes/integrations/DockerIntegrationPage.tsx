import { ContainerGrid } from './ContainerGrid'
import { ContainerGridCard } from './ContainerGridCard'
import { PendingSuggestionsPanel } from './PendingSuggestionsPanel'
import { RecentActionsPanel } from './RecentActionsPanel'

export function DockerIntegrationPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Docker integration</h1>
        <p className="text-sm text-muted-foreground">Container inventory, health, and actions.</p>
      </div>

      {/* Container grid — desktop table + mobile cards */}
      <div className="space-y-2">
        {/* SCAFFOLDING: STAGE-003-004 replaces these empty arrays with a useListContainers() hook */}
        {/* TODO STAGE-003-004: both grids render the full container list at all viewports; CSS hides one. Acceptable for skeleton, revisit if rendering becomes expensive. */}
        <ContainerGrid containers={[]} />
        <ContainerGridCard containers={[]} />
      </div>

      <PendingSuggestionsPanel />
      <RecentActionsPanel />
    </div>
  )
}
