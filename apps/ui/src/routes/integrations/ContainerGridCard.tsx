import { EmptyState } from '@/components/EmptyState'

import type { ContainerRow } from './types'
import { StatusBadge, HealthcheckBadge } from './badges'
import { extractComposeBasename } from './composeBasename'
import { ProbesBadge } from './ProbesBadge'
import { ImageUpdateBadge } from './ImageUpdateBadge'
import { formatDigest } from '@/lib/digest'

interface ContainerGridCardProps {
  containers: ContainerRow[]
}

export function ContainerGridCard({ containers }: ContainerGridCardProps) {
  if (containers.length === 0) {
    return (
      <EmptyState testId="containers-mobile" className="md:hidden">
        No containers discovered yet.
      </EmptyState>
    )
  }
  return (
    <ul className="space-y-2 md:hidden" data-testid="containers-mobile">
      {containers.map((c) => {
        const composeBasename = extractComposeBasename(c.compose_file_path)

        return (
          <li key={c.id} className="rounded-md border border-border bg-card p-3 text-sm">
            <div className="font-medium">{c.name}</div>
            <div className="mt-1 space-y-1 text-xs text-muted-foreground">
              <div title={c.compose_file_path ?? undefined}>Compose: {composeBasename ?? '—'}</div>
              <div className="flex items-center gap-1">
                <span>Status:</span>
                {c.status ? <StatusBadge status={c.status} /> : <span>—</span>}
              </div>
              <div title={c.image ?? undefined}>Image: {formatDigest(c.image)}</div>
              <div>
                CPU: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
                {c.cpu_pct != null ? `${c.cpu_pct.toFixed(1)}%` : '—'}
              </div>
              <div>
                RAM: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
                {c.mem_mib != null ? `${c.mem_mib} MiB` : '—'}
              </div>
              <div
                title={
                  c.restart_count != null && c.restart_count > 0
                    ? `Cumulative: ${c.restart_count}`
                    : undefined
                }
              >
                Restarts (24h):{' '}
                {c.restart_count_24h != null && c.restart_count_24h > 0 ? c.restart_count_24h : '—'}
              </div>
              <div className="flex items-center gap-1">
                <span>Healthcheck:</span>
                {c.healthcheck ? <HealthcheckBadge status={c.healthcheck} /> : <span>—</span>}
              </div>
              <div>
                Image Update: <ImageUpdateBadge containerName={c.name} />
              </div>
              <div>
                Probes: <ProbesBadge containerName={c.name} />
              </div>
              <div>
                {/* SCAFFOLDING: STAGE-003-010 populates Pull & Restart confirm-gated action */}
                Actions: {'—'}
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
