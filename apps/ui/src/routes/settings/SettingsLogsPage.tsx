import { useState } from 'react'

import { useLogsRetention, useUpdateLogsRetention } from '@/api/settingsLogs'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

const MIN_DAYS = 1
const MAX_DAYS = 365

export function SettingsLogsPage() {
  const retention = useLogsRetention()
  const update = useUpdateLogsRetention()
  const [draft, setDraft] = useState<string>('')

  if (retention.isLoading) {
    return (
      <div data-testid="settings-logs-page" className="text-sm text-muted-foreground">
        Loading retention settings…
      </div>
    )
  }
  if (retention.error !== null || retention.data === undefined) {
    return (
      <div data-testid="settings-logs-page" className="text-sm text-destructive">
        Failed to load retention settings.
      </div>
    )
  }

  const data = retention.data
  // Seed the input from the effective value the first time we have data.
  const inputValue =
    draft === '' ? String(data.pending_retention_days ?? data.retention_days) : draft

  const clamp = (n: number): number => Math.min(MAX_DAYS, Math.max(MIN_DAYS, n))

  const handleSave = (): void => {
    const parsed = Number.parseInt(inputValue, 10)
    if (!Number.isFinite(parsed)) return
    update.mutate({ retention_days: clamp(parsed) })
  }

  const diskUsedColor = cn(
    data.disk_used_pct >= data.crit_pct
      ? 'text-destructive'
      : data.disk_used_pct >= data.warn_pct
        ? 'text-amber-500'
        : 'text-foreground',
  )

  return (
    <div data-testid="settings-logs-page" className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Log retention</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm">
            Current:{' '}
            <span data-testid="retention-current" className="font-medium">
              {data.retention_days} days
            </span>{' '}
            (source: <span data-testid="retention-source">{data.retention_source}</span>)
          </p>
          {data.pending_retention_days != null ? (
            <p className="text-sm">
              Pending:{' '}
              <span data-testid="retention-pending" className="font-medium">
                {data.pending_retention_days} days
              </span>
            </p>
          ) : null}
          {data.restart_required ? (
            <div
              data-testid="restart-required-banner"
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400"
            >
              Restart required: recreate the monitor stack (<code>docker compose up -d</code>) to
              apply the new retention.
            </div>
          ) : null}
          <div className="flex items-end gap-2">
            <div className="space-y-1">
              <Label htmlFor="retention-input">Retention (days)</Label>
              <Input
                id="retention-input"
                data-testid="retention-input"
                type="number"
                min={MIN_DAYS}
                max={MAX_DAYS}
                value={inputValue}
                onChange={(e) => {
                  setDraft(e.target.value)
                }}
                className="w-32"
              />
            </div>
            <Button data-testid="retention-save" onClick={handleSave} disabled={update.isPending}>
              {update.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Disk usage</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {!data.disk_budget_available ? (
            <p className="text-sm text-muted-foreground" data-testid="disk-unavailable">
              Disk usage unavailable (budget not configured or config error)
            </p>
          ) : (
            <p className="text-sm">
              Used:{' '}
              <span data-testid="disk-used" className={cn('font-medium', diskUsedColor)}>
                {data.disk_used_gb.toFixed(2)} GiB
              </span>{' '}
              (
              <span data-testid="disk-pct" className={diskUsedColor}>
                {data.disk_used_pct.toFixed(1)}%
              </span>{' '}
              of budget)
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Warning at {data.warn_pct}% / Critical at {data.crit_pct}%
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Per-stream caps</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Configured in <code>homelab-monitor.yaml</code> — edit the file and restart to change.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
