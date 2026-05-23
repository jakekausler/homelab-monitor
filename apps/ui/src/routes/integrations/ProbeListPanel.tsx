import type { ReactNode } from 'react'
import { useToggleProbe } from '@/api/docker'
import type { Schema } from '@/api/types'
import { formatRelative, formatAbsolute } from '@/lib/relativeTime'
import { useNowTick } from '@/lib/useNowTick'

type ProbeRow = Schema<'ProbeRow'>

const SOURCE_LABEL: Record<string, string> = {
  label: 'Label',
  file_override: 'Config file',
  auto_default: 'Auto-detect',
  discovered_accepted: 'Accepted',
}
const sourceLabel = (s: string) => SOURCE_LABEL[s] ?? s

interface ProbeListPanelProps {
  probes: ProbeRow[]
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

function statusBadge(probe: ProbeRow): ReactNode {
  // TODO: migrate to theme-aware status tokens once design system has them
  if (probe.last_status === 'ok') {
    return <span className="rounded bg-green-50 px-2 py-0.5 text-xs text-green-800">OK</span>
  }
  if (probe.last_status === 'fail') {
    return <span className="rounded bg-red-50 px-2 py-0.5 text-xs text-red-800">FAILING</span>
  }
  return <span className="text-xs text-muted-foreground">—</span>
}

export function ProbeListPanel({ probes }: ProbeListPanelProps) {
  const toggle = useToggleProbe()
  const nowMs = useNowTick(1000)

  return (
    <>
      {/* Desktop table */}
      <div className="hidden md:block">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="px-3 py-2">Kind</th>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Source</th>
              <th className="px-3 py-2">Target</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Last error</th>
              <th className="px-3 py-2">Last run</th>
              <th className="px-3 py-2">Enabled</th>
            </tr>
          </thead>
          <tbody>
            {probes.map((p) => (
              <tr key={p.id} className="hover:bg-accent/30">
                <td className="px-3 py-2">{p.kind}</td>
                <td className="px-3 py-2 font-medium">{p.name}</td>
                <td className="px-3 py-2 text-xs">
                  <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">
                    {sourceLabel(p.config_source)}
                  </span>
                </td>
                <td
                  className="px-3 py-2 max-w-[24rem] truncate text-xs text-muted-foreground"
                  title={p.target_value}
                >
                  {p.target_value}
                </td>
                <td className="px-3 py-2">{statusBadge(p)}</td>
                <td
                  className="px-3 py-2 max-w-[16rem] truncate text-xs text-muted-foreground"
                  title={p.last_error ?? undefined}
                >
                  {p.last_error ?? '—'}
                </td>
                <td
                  className="px-3 py-2 text-xs text-muted-foreground"
                  title={formatAbsolute(p.last_run_at)}
                >
                  {formatRelative(p.last_run_at, nowMs)}
                </td>
                <td className="px-3 py-2">
                  <button
                    type="button"
                    className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-accent"
                    onClick={() => toggle.mutate({ probeId: p.id, enabled: !p.enabled })}
                    disabled={toggle.isPending}
                  >
                    {p.enabled ? 'Disable' : 'Enable'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile cards */}
      <ul className="space-y-2 md:hidden">
        {probes.map((p) => (
          <li key={p.id} className="rounded-md border border-border bg-card p-3 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div className="font-medium">
                {p.kind}.{p.name}
              </div>
              {statusBadge(p)}
            </div>
            <div className="mt-1 space-y-1 text-xs text-muted-foreground">
              <div>
                Source:{' '}
                <span
                  aria-label={`Source: ${sourceLabel(p.config_source)}`}
                  className="rounded bg-muted px-1.5 py-0.5"
                >
                  {sourceLabel(p.config_source)}
                </span>
              </div>
              <div title={p.target_value}>
                {p.kind === 'exec' ? 'Command' : 'Target'}:{' '}
                <code className="text-xs">{truncate(p.target_value, 60)}</code>
              </div>
              {p.last_error && (
                <div className="truncate" title={p.last_error}>
                  Error: {p.last_error}
                </div>
              )}
              <div title={formatAbsolute(p.last_run_at)}>
                Last run: {formatRelative(p.last_run_at, nowMs)}
              </div>
            </div>
            <div className="mt-2">
              <button
                type="button"
                className="rounded border border-border bg-card px-2 py-1 text-xs hover:bg-accent"
                onClick={() => toggle.mutate({ probeId: p.id, enabled: !p.enabled })}
                disabled={toggle.isPending}
              >
                {p.enabled ? 'Disable' : 'Enable'}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </>
  )
}
