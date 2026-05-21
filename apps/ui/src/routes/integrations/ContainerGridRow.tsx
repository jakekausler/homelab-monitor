import type { ContainerRow } from './types'

interface ContainerGridRowProps {
  container: ContainerRow
}

export function ContainerGridRow({ container }: ContainerGridRowProps) {
  return (
    <tr className="hover:bg-accent/30">
      <td className="px-3 py-2 font-medium">{container.name}</td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
        {container.status ?? '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
        {container.image ?? '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
        {container.cpu_pct != null ? `${container.cpu_pct.toFixed(1)}%` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
        {container.mem_mib != null ? `${container.mem_mib} MiB` : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-008/009 populate image-update badges */}
        {'—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {/* SCAFFOLDING: STAGE-003-004 populates from docker socket collector */}
        {container.healthcheck ?? '—'}
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
