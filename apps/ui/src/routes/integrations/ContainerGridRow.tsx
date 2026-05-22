import type { ContainerRow } from './types'
import { StatusBadge, HealthcheckBadge } from './badges'
import { extractComposeBasename } from './composeBasename'
import { ProbesBadge } from './ProbesBadge'

interface ContainerGridRowProps {
  container: ContainerRow
}

export function ContainerGridRow({ container }: ContainerGridRowProps) {
  // Parent directory basename: "/.../homelab-monitor/docker-compose.yml" → "homelab-monitor"
  const composeBasename = extractComposeBasename(container.compose_file_path)

  return (
    <tr className="hover:bg-accent/30">
      <td
        className="px-3 py-2 text-xs text-muted-foreground"
        title={container.compose_file_path ?? undefined}
      >
        {composeBasename ?? '—'}
      </td>
      <td className="px-3 py-2 font-medium">{container.name}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {container.status ? <StatusBadge status={container.status} /> : '—'}
      </td>
      <td
        className="px-3 py-2 text-right tabular-nums text-xs text-muted-foreground"
        title={
          container.restart_count != null && container.restart_count > 0
            ? `Cumulative: ${container.restart_count}`
            : undefined
        }
      >
        {container.restart_count_24h != null && container.restart_count_24h > 0
          ? container.restart_count_24h
          : '—'}
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
        <ProbesBadge containerName={container.name} />
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
