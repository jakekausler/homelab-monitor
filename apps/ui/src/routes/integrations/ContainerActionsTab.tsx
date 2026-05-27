import { useParams } from '@tanstack/react-router'
import { RecentActionsPanel } from './RecentActionsPanel'

export function ContainerActionsTab() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : ''

  return (
    <div className="space-y-4">
      <RecentActionsPanel containerName={containerName} />
    </div>
  )
}
