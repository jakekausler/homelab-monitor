import { useState, useMemo } from 'react'
import { ContainerGrid } from './ContainerGrid'
import { ContainerGridCard } from './ContainerGridCard'
import { PendingSuggestionsPanel } from './PendingSuggestionsPanel'
import { useListContainers } from '@/api/docker'

export function DockerIntegrationPage() {
  const result = useListContainers()
  const allContainers = useMemo(() => result.data?.containers ?? [], [result.data])
  const [showMissing, setShowMissing] = useState(false)

  const missingCount = useMemo(
    () => allContainers.filter((c) => c.status === 'missing').length,
    [allContainers],
  )
  const containers = useMemo(
    () => (showMissing ? allContainers : allContainers.filter((c) => c.status !== 'missing')),
    [allContainers, showMissing],
  )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Docker integration</h1>
        <p className="text-sm text-muted-foreground">Container inventory, health, and actions.</p>
      </div>

      {/* Container grid — desktop table + mobile cards */}
      <div className="space-y-2">
        {result.error && result.error.status === 503 && (
          <div className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800">
            Container data temporarily unavailable
          </div>
        )}
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={showMissing}
            onChange={(e) => setShowMissing(e.target.checked)}
            aria-label="Show missing containers"
            data-testid="show-missing-toggle"
          />
          <span>
            Show missing containers
            {missingCount > 0 ? ` (${missingCount})` : ''}
          </span>
        </label>
        {/* TODO: both grids render the full container list at all viewports; CSS hides one. Acceptable for skeleton, revisit if rendering becomes expensive. */}
        <ContainerGrid containers={containers} />
        <ContainerGridCard containers={containers} />
      </div>

      <PendingSuggestionsPanel />
    </div>
  )
}
