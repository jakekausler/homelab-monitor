import { EmptyState } from '@/components/EmptyState'

import type { ContainerRow } from './types'

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
      {containers.map((c) => (
        <li key={c.id} className="rounded-md border border-border bg-card p-3 text-sm">
          <div className="font-medium">{c.name}</div>
          <div className="mt-1 space-y-1 text-xs text-muted-foreground">
            <div>
              Status: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
              {c.status ?? '—'}
            </div>
            <div>
              Image: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
              {c.image ?? '—'}
            </div>
            <div>
              CPU: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
              {c.cpu_pct != null ? `${c.cpu_pct.toFixed(1)}%` : '—'}
            </div>
            <div>
              RAM: {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
              {c.mem_mib != null ? `${c.mem_mib} MiB` : '—'}
            </div>
            <div>
              {/* SCAFFOLDING: STAGE-003-008/009 populate image-update badges */}
              Image Update: {'—'}
            </div>
            <div>
              {/* SCAFFOLDING: STAGE-003-006/007 populate label-based probe badges */}
              Probes: {'—'}
            </div>
            <div>
              {/* SCAFFOLDING: STAGE-003-010 populates Pull & Restart confirm-gated action */}
              Actions: {'—'}
            </div>
          </div>
        </li>
      ))}
    </ul>
  )
}
