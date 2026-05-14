import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useGetCron, useHideCron, useUpdateCron } from '@/api/crons'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { StateBadge } from '@/components/crons/badges'
import { CronForm } from '@/components/crons/CronForm'
import { formatAbsolute, formatRelative } from '@/lib/relativeTime'

type CronUpdate = Schema<'CronUpdate'>

export interface CronDetailProps {
  fingerprint: string
}

export function CronDetail({ fingerprint }: CronDetailProps) {
  const detail = useGetCron(fingerprint, { includeHidden: true })
  const update = useUpdateCron(fingerprint)
  const hide = useHideCron(fingerprint)

  if (detail.isLoading) {
    return <p className="text-muted-foreground">Loading cron…</p>
  }
  if (detail.error) {
    return (
      <p role="alert" className="text-red-600">
        {detail.error.message}
      </p>
    )
  }
  if (!detail.data) {
    return <p className="text-muted-foreground">Cron not found.</p>
  }

  const cron = detail.data.cron
  const state = detail.data.state
  const isHidden = cron.hidden_at !== null
  const isRemote = cron.source_path === null

  const handleSave = async (body: CronUpdate) => {
    try {
      await update.mutateAsync(body)
      toast.success('Cron updated')
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Update failed'
      toast.error(msg)
    }
  }

  const handleHide = async () => {
    try {
      await hide.mutateAsync()
      toast.success('Cron hidden')
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Hide failed'
      toast.error(msg)
    }
  }

  const handleUnhide = async () => {
    try {
      await update.mutateAsync({ hidden_at: null })
      toast.success('Cron restored')
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Restore failed'
      toast.error(msg)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">{cron.name}</h1>
          <StateBadge state={cron.last_seen_state} />
          {isRemote && <Badge variant="secondary">Remote</Badge>}
          {isHidden && <Badge variant="muted">Hidden</Badge>}
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          {cron.host} · <span className="font-mono">{cron.command}</span>
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <HeartbeatStatePanel cron={cron} state={state} />
        <DiskSourcePanel cron={cron} isRemote={isRemote} />
        <MonitoringPolicyPanel cron={cron} onSave={handleSave} isSubmitting={update.isPending} />
        <ActionsPanel
          isHidden={isHidden}
          onHide={handleHide}
          onUnhide={handleUnhide}
          hidePending={hide.isPending}
          unhidePending={update.isPending}
        />
      </div>
    </div>
  )
}

function WrapperRow({ at }: { at: string | null }) {
  return (
    <Row label="Wrapper">
      {at !== null ? (
        <span title={formatAbsolute(at)}>Wrapper last seen {formatRelative(at)}</span>
      ) : (
        <span className="text-muted-foreground">
          No wrapper installed (heartbeats from ad-hoc curl)
        </span>
      )}
    </Row>
  )
}

function HeartbeatStatePanel({
  cron,
  state,
}: {
  cron: Schema<'CronOut'>
  state: Schema<'HeartbeatStateOut'> | null
}) {
  return (
    <Card aria-labelledby="panel-heartbeat-state">
      <CardHeader>
        <CardTitle id="panel-heartbeat-state">Heartbeat state</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {state ? (
          <>
            <Row label="Current">
              <StateBadge state={state.current_state} />
            </Row>
            <Row label="Streak">{state.current_streak}</Row>
            <Row label="Last OK">{formatRelative(state.last_ok_at)}</Row>
            <Row label="Last Fail">{formatRelative(state.last_fail_at)}</Row>
            <Row label="Next due">{formatRelative(state.expected_next_at)}</Row>
            {state.last_duration_seconds !== null && (
              <Row label="Last duration">{state.last_duration_seconds}s</Row>
            )}
            {state.last_exit_code !== null && (
              <Row label="Last exit code">{state.last_exit_code}</Row>
            )}
            <WrapperRow at={cron.wrapper_last_seen_at} />
            {/* TODO(STAGE-002-009): append install method when wrapper_install_method ships */}
          </>
        ) : (
          <>
            <p className="text-muted-foreground">No pings received yet.</p>
            <WrapperRow at={cron.wrapper_last_seen_at} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

function DiskSourcePanel({ cron, isRemote }: { cron: Schema<'CronOut'>; isRemote: boolean }) {
  return (
    <Card aria-labelledby="panel-disk-source">
      <CardHeader>
        <CardTitle id="panel-disk-source">Disk source</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {isRemote && (
          <div
            role="note"
            className="rounded-md border border-blue-500/30 bg-blue-500/10 p-3 text-blue-900 dark:text-blue-200"
            data-testid="remote-banner"
          >
            Remote cron on <span className="font-mono">{cron.host}</span>. The monitor doesn't have
            direct file access to this host. Wrapper-based heartbeats are the only signal.
          </div>
        )}
        <Row label="Host">{cron.host}</Row>
        <Row label="Source path">
          {cron.source_path !== null ? (
            <span className="font-mono">{cron.source_path}</span>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </Row>
        <Row label="Schedule">
          <span className="font-mono" title={cron.schedule_canonical ?? undefined}>
            {cron.schedule ?? `every ${String(cron.cadence_seconds)}s`}
          </span>
        </Row>
        <Row label="Command">
          <span className="font-mono break-all">{cron.command}</span>
        </Row>
        <Row label="Last discovered">
          {cron.last_discovered_at !== null ? (
            <span title={formatAbsolute(cron.last_discovered_at)}>
              {formatRelative(cron.last_discovered_at)}
            </span>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </Row>
      </CardContent>
    </Card>
  )
}

function MonitoringPolicyPanel({
  cron,
  onSave,
  isSubmitting,
}: {
  cron: Schema<'CronOut'>
  onSave: (body: Schema<'CronUpdate'>) => Promise<void>
  isSubmitting: boolean
}) {
  return (
    <Card aria-labelledby="panel-monitoring-policy">
      <CardHeader>
        <CardTitle id="panel-monitoring-policy">Monitoring policy</CardTitle>
      </CardHeader>
      <CardContent>
        <CronForm
          defaultValues={cron}
          onSubmit={onSave}
          isSubmitting={isSubmitting}
          submitLabel="Save changes"
        />
      </CardContent>
    </Card>
  )
}

function ActionsPanel({
  isHidden,
  onHide,
  onUnhide,
  hidePending,
  unhidePending,
}: {
  isHidden: boolean
  onHide: () => Promise<void>
  onUnhide: () => Promise<void>
  hidePending: boolean
  unhidePending: boolean
}) {
  return (
    <Card aria-labelledby="panel-actions">
      <CardHeader>
        <CardTitle id="panel-actions">Actions</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Row 1: Hide / Unhide */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">{isHidden ? 'Restore cron' : 'Hide cron'}</p>
            <p className="text-xs text-muted-foreground">
              {isHidden
                ? 'Restore this cron to the default views and alert routing.'
                : 'Hide this cron from default views and suppress its alerts.'}
            </p>
          </div>
          {isHidden ? (
            <Button onClick={() => void onUnhide()} disabled={unhidePending}>
              {unhidePending ? 'Unhiding…' : 'Unhide'}
            </Button>
          ) : (
            <Button variant="destructive" onClick={() => void onHide()} disabled={hidePending}>
              {hidePending ? 'Hiding…' : 'Hide'}
            </Button>
          )}
        </div>

        <hr className="border-border" />

        {/* Row 2: Install heartbeat wrapper (disabled) */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">Install heartbeat wrapper</p>
            <p className="text-xs text-muted-foreground">
              Replace ad-hoc heartbeats with a managed wrapper script.
            </p>
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              {/* Wrap disabled button in a span so the tooltip can still trigger on hover.
                  Radix Tooltip TriggerAsChild w/ disabled button: wrap in span to keep events. */}
              <span tabIndex={0}>
                <Button disabled aria-label="Install heartbeat wrapper">
                  Install heartbeat wrapper
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              Local install ships in STAGE-002-009. Remote install requires cross-host work in
              EPIC-015 / EPIC-017.
            </TooltipContent>
          </Tooltip>
        </div>
        {/* TODO(STAGE-002-010): render wrapper-health badge from vmalert / recorded label */}
      </CardContent>
    </Card>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-sm">{children}</span>
    </div>
  )
}
