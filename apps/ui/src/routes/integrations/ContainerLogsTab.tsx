import { useNavigate, useParams, useSearch } from '@tanstack/react-router'

import { DockerContainerLogsViewerBody } from './DockerContainerLogsViewerBody'

export function ContainerLogsTab() {
  const { name } = useParams({ strict: false })
  const containerName = typeof name === 'string' && name.length > 0 ? name : ''
  const search = useSearch({ strict: false })
  const navigate = useNavigate()

  const setSearch = (next: {
    since?: string | undefined
    start?: string | undefined
    end?: string | undefined
  }): void => {
    void navigate({
      to: '/integrations/docker/containers/$name/logs',
      params: { name: containerName },
      search: next,
    })
  }

  return (
    <div className="space-y-4">
      <DockerContainerLogsViewerBody
        containerName={containerName}
        since={search.since}
        start={search.start}
        end={search.end}
        onRangeChange={setSearch}
      />
    </div>
  )
}
