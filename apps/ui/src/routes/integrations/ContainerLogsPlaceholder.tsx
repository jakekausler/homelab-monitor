import { Link, useParams } from '@tanstack/react-router'

import { EmptyState } from '@/components/EmptyState'

export function ContainerLogsPlaceholderPage() {
  // SCAFFOLDING: STAGE-003-011 swaps this with the real per-container log viewer
  // (mirrors CronRunLogViewer pattern: fetches from /api/integrations/docker/containers/{name}/logs,
  // reuses VictoriaLogsClient from STAGE-002-013)
  const params = useParams({ strict: false })
  const name = params.name ?? ''

  return (
    <div className="space-y-4">
      <Link
        to="/integrations/docker"
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        ← Back to Docker integration
      </Link>
      <EmptyState>
        Log viewer for <code className="font-mono">{name}</code> not yet implemented.
      </EmptyState>
    </div>
  )
}
