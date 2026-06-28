import { Link, Outlet } from '@tanstack/react-router'
import type { JSX } from 'react'

import { ErrorDisplay } from '@/components/ErrorDisplay'
import { Badge } from '@/components/ui/badge'
import { useSurveillanceSummary } from '@/api/surveillance'

const TABS = [{ path: '/integrations/surveillance/cameras', label: 'Cameras' }] as const
// SCAFFOLDING: STAGE-008-029 adds an "Activity" tab here.

type BadgeVariant = 'ok' | 'warn' | 'critical' | 'muted'

/** License chip label from used/max (either null -> "License —"). */
function licenseLabel(used: number | null, max: number | null): string {
  if (used === null || max === null) return 'License —'
  return `License ${Math.round(used)}/${Math.round(max)}`
}

/** Cameras chip: connected/total; ok when all connected, warn otherwise. */
function camerasChip(
  connected: number | null,
  total: number | null,
): { label: string; variant: BadgeVariant } {
  if (total === null) return { label: 'Cameras —', variant: 'muted' }
  const conn = connected ?? 0
  return {
    label: `Cameras ${Math.round(conn)}/${Math.round(total)}`,
    variant: conn === total ? 'ok' : 'warn',
  }
}

function SurveillanceStatusStrip(): JSX.Element {
  const result = useSurveillanceSummary()
  const data = result.data
  const cameras = data ? camerasChip(data.cameras_connected_total, data.cameras_total) : null

  return (
    <div data-testid="surveillance-status-strip" className="px-4 pt-2">
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          Surveillance metrics temporarily unavailable
        </div>
      )}
      {result.isError && result.error.status !== 502 && <ErrorDisplay error={result.error} />}
      {data && !data.data_available && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="muted">Surveillance collector has not run yet</Badge>
        </div>
      )}
      {data && data.data_available && cameras && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="muted">{licenseLabel(data.license_used, data.license_max)}</Badge>
          <Badge variant={data.homemode_on ? 'warn' : 'ok'}>
            HomeMode: {data.homemode_on ? 'On' : 'Off'}
          </Badge>
          <Badge variant={cameras.variant}>{cameras.label}</Badge>
        </div>
      )}
    </div>
  )
}

export function SurveillanceLayout(): JSX.Element {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="px-4 pt-4">
        <h1 className="text-lg font-semibold">Surveillance</h1>
        <p className="text-sm text-muted-foreground">
          Synology Surveillance Station — cameras, license, and Home Mode.
        </p>
      </div>
      <SurveillanceStatusStrip />
      <nav
        aria-label="Surveillance tabs"
        data-testid="surveillance-tabs"
        className="flex gap-1 border-b border-border px-4 pt-2"
      >
        {TABS.map((tab) => (
          <Link
            key={tab.path}
            to={tab.path}
            data-testid={`surveillance-tab-${tab.path.split('/').pop()}`}
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
