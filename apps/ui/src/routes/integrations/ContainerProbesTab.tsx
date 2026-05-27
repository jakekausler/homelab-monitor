import { useParams } from '@tanstack/react-router'
import { useListContainers } from '@/api/docker'
import { ContainerProbesCard } from './ContainerProbesCard'

export function ContainerProbesTab() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : ''
  const listResult = useListContainers()
  const container = listResult.data?.containers.find((c) => c.name === containerName) ?? null

  if (!container) {
    return (
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          {listResult.isPending ? 'Loading…' : 'Container not found.'}
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <ContainerProbesCard container={container} />
    </div>
  )
}
