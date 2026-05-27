import { useMemo } from 'react'
import { ContainerGrid } from './ContainerGrid'
import { ContainerGridCard } from './ContainerGridCard'
import { ProbesPanel } from './ProbesPanel'
import { useListContainers } from '@/api/docker'

export function DockerIntegrationPage() {
  const result = useListContainers()
  const containers = useMemo(() => result.data?.containers ?? [], [result.data])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Docker integration</h1>
        <p className="text-sm text-muted-foreground">Container inventory, health, and actions.</p>
      </div>

      <div className="space-y-2">
        {result.error && result.error.status === 503 && (
          <div className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800">
            Container data temporarily unavailable
          </div>
        )}
        {/* TODO: both grids render the full container list at all viewports; CSS hides one. Acceptable for skeleton, revisit if rendering becomes expensive. */}
        <ContainerGrid containers={containers} />
        <ContainerGridCard containers={containers} />
      </div>

      <ProbesPanel />
    </div>
  )
}
