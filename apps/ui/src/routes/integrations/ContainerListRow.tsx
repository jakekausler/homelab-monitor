import { Link } from '@tanstack/react-router'
import { ChevronRight, RotateCcw, Cpu, MemoryStick } from 'lucide-react'

import type { ContainerRow } from './types'
import { StatusBadge, HealthcheckBadge } from './badges'
import { ProbesBadge } from './ProbesBadge'
import { ImageUpdateBadge } from './ImageUpdateBadge'
import { formatDigest } from '@/lib/digest'

interface ContainerListRowProps {
  container: ContainerRow
}

export function ContainerListRow({ container }: ContainerListRowProps) {
  return (
    <Link
      to="/integrations/docker/containers/$name/overview"
      params={{ name: container.name }}
      className="block rounded-md border border-border bg-card p-3 hover:bg-accent/20 transition-colors"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
        {/* Left: Name and image */}
        <div className="flex-1 min-w-0">
          <div className="font-medium text-foreground">{container.name}</div>
          <div
            className="text-xs text-muted-foreground truncate"
            title={container.image ?? undefined}
          >
            {formatDigest(container.image)}
          </div>
        </div>

        {/* Right: Badges, metrics, and chevron */}
        <div className="flex flex-col gap-2 sm:items-end">
          {/* Badges row */}
          <div className="flex flex-wrap gap-1 sm:justify-end">
            {container.status && <StatusBadge status={container.status} />}
            <ImageUpdateBadge containerName={container.name} />
            <ProbesBadge containerName={container.name} />
            {container.healthcheck && <HealthcheckBadge status={container.healthcheck} />}
          </div>

          {/* Restart chip and metrics row */}
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground sm:flex-nowrap">
            {container.restart_count_24h != null && container.restart_count_24h > 0 && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-1 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
                <RotateCcw className="size-3.5" />
                {container.restart_count_24h} restart{container.restart_count_24h !== 1 ? 's' : ''}{' '}
                (24h)
              </span>
            )}
            {container.cpu_pct != null && (
              <span className="inline-flex items-center gap-1">
                <Cpu className="size-3.5" />
                {container.cpu_pct.toFixed(1)}%
              </span>
            )}
            {container.mem_mib != null && (
              <span className="inline-flex items-center gap-1">
                <MemoryStick className="size-3.5" />
                {container.mem_mib.toFixed(0)} MiB
              </span>
            )}
            <ChevronRight className="size-4" />
          </div>
        </div>
      </div>
    </Link>
  )
}
