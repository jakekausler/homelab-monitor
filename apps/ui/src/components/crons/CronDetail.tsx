import { useState } from 'react'
import { useNavigate } from '@tanstack/react-router'

import { ApiError } from '@/api/client'
import { useGetCron, useSoftDeleteCron, useUpdateCron } from '@/api/crons'
import type { Schema } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { StateBadge } from '@/components/crons/badges'
import { CronForm } from '@/components/crons/CronForm'
import { SchedulePreviewForSaved } from '@/components/crons/SchedulePreview'
import { formatAbsolute, formatRelative } from '@/lib/relativeTime'

type CronUpdate = Schema<'CronUpdate'>

export interface CronDetailProps {
  fingerprint: string
}

export function CronDetail({ fingerprint }: CronDetailProps) {
  const navigate = useNavigate()
  const detail = useGetCron(fingerprint, { includeHidden: true })
  const update = useUpdateCron(fingerprint)
  const softDelete = useSoftDeleteCron(fingerprint)
  const [editError, setEditError] = useState<string | null>(null)

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

  const handleSave = async (body: CronUpdate) => {
    setEditError(null)
    try {
      await update.mutateAsync(body)
      void navigate({
        to: '/inventory/crons',
        search: {
          page: 1,
          page_size: 100,
          host: undefined,
          enabled: undefined,
          state: undefined,
          q: undefined,
          include_hidden: false,
        },
      })
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : 'Update failed')
    }
  }

  const handleRestore = async () => {
    setEditError(null)
    try {
      await update.mutateAsync({ hidden_at: null })
    } catch (err) {
      setEditError(err instanceof ApiError ? err.message : 'Restore failed')
    }
  }

  const handleDelete = async () => {
    try {
      await softDelete.mutateAsync()
      // include_hidden:true keeps the just-archived row visible on the list
      // (with its `archived` badge); otherwise the row vanishes and the user
      // can't tell whether the action took effect.
      void navigate({
        to: '/inventory/crons',
        search: {
          page: 1,
          page_size: 100,
          host: undefined,
          enabled: undefined,
          state: undefined,
          q: undefined,
          include_hidden: true,
        },
      })
    } catch (err) {
      // TODO(STAGE-002-006): surface Archive failures via toast/snackbar.
      // Currently silent (errors only logged); user retried via Restore from the list.
      console.error('soft-delete failed', err)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">{cron.name}</h1>
            <StateBadge state={cron.last_seen_state} />
            {isHidden && (
              <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                archived
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {cron.host} · <span className="font-mono">{cron.command}</span>
          </p>
        </div>
        <div className="flex gap-2">
          {isHidden ? (
            <Button onClick={() => void handleRestore()} disabled={update.isPending}>
              Restore
            </Button>
          ) : (
            <Button
              variant="destructive"
              onClick={() => void handleDelete()}
              disabled={softDelete.isPending}
            >
              {softDelete.isPending ? 'Archiving…' : 'Archive'}
            </Button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Edit</CardTitle>
          </CardHeader>
          <CardContent>
            <CronForm
              defaultValues={cron}
              onSubmit={handleSave}
              errorMessage={editError}
              isSubmitting={update.isPending}
              submitLabel="Save changes"
            />
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Heartbeat state</CardTitle>
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
                  <Row label="Next due">{formatAbsolute(state.expected_next_at)}</Row>
                </>
              ) : (
                <p className="text-muted-foreground">No pings received yet.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Next runs</CardTitle>
            </CardHeader>
            <CardContent>
              {cron.schedule !== null && cron.schedule !== '' ? (
                <SchedulePreviewForSaved fingerprint={cron.fingerprint} count={3} />
              ) : (
                <p className="text-sm text-muted-foreground">Cadence-based; no schedule preview.</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
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
