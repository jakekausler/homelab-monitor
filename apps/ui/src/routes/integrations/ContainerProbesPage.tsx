import { Link, useParams } from '@tanstack/react-router'

import { useListProbes } from '@/api/docker'
import { EmptyState } from '@/components/EmptyState'
import { ErrorDisplay } from '@/components/ErrorDisplay'

import { ProbeListPanel } from './ProbeListPanel'

export function ContainerProbesPage() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' ? name : ''
  const result = useListProbes(containerName)

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <Link to="/integrations/docker" className="text-xs text-muted-foreground hover:underline">
            ← Back to Docker integration
          </Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">Probes for {containerName}</h1>
        </div>
      </div>

      {result.isError && <ErrorDisplay error={result.error} />}
      {result.isPending && <div className="text-sm text-muted-foreground">Loading probes…</div>}
      {result.data && result.data.probes.length === 0 && (
        <EmptyState testId="probes-empty">No probes configured for this container.</EmptyState>
      )}
      {result.data && result.data.probes.length > 0 && (
        <ProbeListPanel probes={result.data.probes} />
      )}
    </div>
  )
}
