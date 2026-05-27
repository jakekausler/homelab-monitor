import { useParams } from '@tanstack/react-router'
import { DockerContainerLogsViewerBody } from './DockerContainerLogsViewerBody'

export function ContainerLogsTab() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : ''

  return (
    <div className="space-y-4">
      <DockerContainerLogsViewerBody containerName={containerName} />
    </div>
  )
}
