import type { ContainerRow } from './types'
import { StatusBadge, HealthcheckBadge, RestartCountBadge } from './badges'

interface ContainerGridRowProps {
  container: ContainerRow
}

export function ContainerGridRow({ container }: ContainerGridRowProps) {
  return (
    <tr className="hover:bg-accent/30">
      <td className="px-3 py-2 font-medium">{container.name}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.status ? (
          <div className="flex items-center gap-2">
            <StatusBadge status={container.status} />
            {container.restart_count != null && (
              <RestartCountBadge count={container.restart_count} />
            )}
          </div>
        ) : (
          '—'
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">{container.image ?? '—'}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.cpu_pct != null ? `${container.cpu_pct.toFixed(1)}%` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.mem_mib != null ? `${container.mem_mib.toFixed(0)} MiB` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-008/009 populate image-update badges */}
        {'—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.healthcheck ? <HealthcheckBadge status={container.healthcheck} /> : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-006/007 populate label-based probe badges */}
        {'—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-011 replaces with "View logs →" link */}
        {'—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-010 populates Pull & Restart confirm-gated action */}
        {'—'}
      </td>
    </tr>
  )
}
