import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'
import { Badge } from '@/components/ui/badge'
import { useSynologySummary } from '@/api/synology'
import { formatRelative } from '@/lib/relativeTime'

const TABS = [
  { path: '/integrations/synology/hardware', label: 'Hardware' },
  { path: '/integrations/synology/ops', label: 'Ops' },
] as const

type BadgeVariant = 'ok' | 'warn' | 'critical' | 'muted'

/**
 * Volume-usage badge variant from the max volume-used percentage.
 * null -> 'muted'; <80 -> 'ok'; 80..90 -> 'warn'; >90 -> 'critical'.
 */
function volumeVariant(percent: number | null): BadgeVariant {
  if (percent === null) return 'muted'
  if (percent > 90) return 'critical'
  if (percent >= 80) return 'warn'
  return 'ok'
}

/**
 * UPS chip label + variant. On battery -> 'warn' (or 'critical' when a known
 * charge is under 20%). Not on battery -> 'ok'. Charge is appended only when known.
 */
function upsChip(
  onBattery: boolean,
  charge: number | null,
): { label: string; variant: BadgeVariant } {
  const chargeSuffix = charge !== null ? ` (${Math.round(charge)}%)` : ''
  if (onBattery) {
    return {
      label: `UPS: on battery${chargeSuffix}`,
      variant: charge !== null && charge < 20 ? 'critical' : 'warn',
    }
  }
  return { label: `UPS: online${chargeSuffix}`, variant: 'ok' }
}

function SynologyStatusStrip(): JSX.Element {
  const result = useSynologySummary()
  const ups = result.data
    ? upsChip(result.data.ups_on_battery, result.data.ups_charge_percent)
    : null

  return (
    <div data-testid="synology-status-strip" className="px-4 pt-2">
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Synology metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {result.data && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={result.data.dsm_up ? 'ok' : 'warn'}>
            System health: {result.data.dsm_up ? 'OK' : 'Degraded'}
          </Badge>
          <Badge variant={volumeVariant(result.data.volume_used_percent_max)}>
            {result.data.volume_used_percent_max === null
              ? 'Volume: —'
              : `Volume: ${Math.round(result.data.volume_used_percent_max)}%`}
          </Badge>
          {ups && <Badge variant={ups.variant}>{ups.label}</Badge>}
          <Badge variant="muted">Updated {formatRelative(result.data.last_seen)}</Badge>
        </div>
      )}
    </div>
  )
}

export function SynologyLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-4 pt-4">
        <h1 className="text-lg font-semibold">Synology integration</h1>
        <p className="text-sm text-muted-foreground">
          Synology DS3622xs+ NAS — storage, hardware, and operations.
        </p>
      </div>
      <SynologyStatusStrip />
      <nav
        aria-label="Synology tabs"
        data-testid="synology-tabs"
        className="flex gap-1 border-b border-border px-4 pt-2"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            data-testid={`synology-tab-${tab.path.split('/').pop()}`}
            className="rounded-t-md px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
            activeProps={{
              className: 'rounded-t-md px-3 py-2 text-sm text-foreground border-b-2 border-primary',
            }}
          >
            {tab.label}
          </Link>
        ))}
      </nav>
      <div className="min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  )
}
