import { Link, Outlet, useParams } from '@tanstack/react-router'
import { ArrowLeft } from 'lucide-react'

import { useListContainers } from '@/api/docker'
import { StatusBadge } from './badges'
import { ContainerTabsNav } from './ContainerTabsNav'

export function ContainerPage() {
  const { name } = useParams({ from: '/protected/integrations/docker/containers/$name' })
  const list = useListContainers()
  const row = list.data?.containers.find((c) => c.name === name) ?? null

  return (
    <div className="space-y-4">
      <div className="space-y-3 border-b border-border pb-3">
        <Link
          to="/integrations/docker"
          className="inline-flex items-center text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="mr-1 size-4" />
          Back to Docker integration
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{name}</h1>
          {row?.status && <StatusBadge status={row.status} />}
        </div>
        {row?.image && (
          <p className="truncate text-xs text-muted-foreground" title={row.image}>
            {row.image}
          </p>
        )}
        <ContainerTabsNav name={name} />
      </div>
      <div className="px-1">
        <Outlet />
      </div>
    </div>
  )
}
