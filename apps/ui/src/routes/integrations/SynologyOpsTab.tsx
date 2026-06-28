import type { JSX } from 'react'

import { useSynologyConnections, useSynologyOps } from '@/api/synology'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'

import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'
import { severityVariant } from './synologyFormat'
import type {
  ConnectionRow,
  MountRow,
  ReplicationRow,
  SynologyBackup,
  SynologyReplication,
  SynologySecurity,
  SynologyUpdates,
} from './synologyFormat'
import { formatBytes } from './unifiFormat'

type SynologyOps = Schema<'SynologyOps'>
type SynologyConnections = Schema<'SynologyConnections'>

function BackupPanel({ backup }: { backup: SynologyBackup }): JSX.Element {
  let statusBadge: JSX.Element
  if (backup.last_result_ok === true) {
    statusBadge = <Badge variant="ok">Last run OK</Badge>
  } else if (backup.last_result_ok === false) {
    statusBadge = <Badge variant="critical">Last run FAILED</Badge>
  } else {
    statusBadge = <Badge variant="muted">No result yet</Badge>
  }
  return (
    <div className="space-y-2 text-sm" data-testid="synology-backup-panel">
      {backup.no_backup_configured ? (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="warn">No backup configured</Badge>
          <span className="text-muted-foreground">No Hyper Backup tasks defined on the NAS.</span>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span>{backup.configured_count} backup tasks configured</span>
          {statusBadge}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Per-job timeline (age/size) not exposed by the DSM collector.
      </p>
    </div>
  )
}

function SecurityPanel({ security }: { security: SynologySecurity }): JSX.Element {
  const safetyBadge = security.security_safe ? (
    <Badge variant="ok">Secure</Badge>
  ) : (
    <Badge variant="warn">Security advisories present</Badge>
  )

  const withCount = security.findings.filter((f) => f.count !== null && f.count > 0)
  const withNull = security.findings.filter((f) => f.count === null)
  const withZero = security.findings.filter((f) => f.count === 0)
  const shown = [...withCount, ...withNull]

  return (
    <div className="space-y-2 text-sm" data-testid="synology-security-panel">
      <div className="flex flex-wrap items-center gap-2">{safetyBadge}</div>
      <div className="min-w-0 flex items-center gap-2 overflow-x-auto whitespace-nowrap">
        {shown.length === 0 ? (
          <span className="text-muted-foreground">No active findings</span>
        ) : (
          shown.map((f) => (
            <Badge
              key={f.severity}
              variant={f.count === null ? 'muted' : severityVariant(f.severity)}
            >
              {f.severity}: {f.count === null ? '—' : f.count}
            </Badge>
          ))
        )}
      </div>
      {withZero.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {withZero.length} other severit{withZero.length === 1 ? 'y' : 'ies'} clear
        </p>
      )}
    </div>
  )
}

function UpdatesPanel({ updates }: { updates: SynologyUpdates }): JSX.Element {
  const dsmBadge = updates.dsm_update_available ? (
    <Badge variant="warn">DSM update available</Badge>
  ) : (
    <Badge variant="ok">DSM up to date</Badge>
  )
  const pending = updates.packages.filter((p) => p.update_available)
  return (
    <div className="space-y-2 text-sm" data-testid="synology-updates-panel">
      <div className="flex flex-wrap items-center gap-2">{dsmBadge}</div>
      {updates.packages_with_updates_count === 0 ? (
        <p className="text-muted-foreground">All packages up to date</p>
      ) : (
        <div className="space-y-2">
          <p>{updates.packages_with_updates_count} package updates available</p>
          <div className="min-w-0 flex items-center gap-2 overflow-x-auto whitespace-nowrap">
            {pending.map((p) => (
              <Badge key={p.package} variant="warn">
                {p.package}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function ReplicationPanel({ replication }: { replication: SynologyReplication }): JSX.Element {
  return (
    <div className="space-y-2 text-sm" data-testid="synology-replication-panel">
      {!replication.replication_available && (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="muted">Replication not enabled</Badge>
          <span className="text-muted-foreground">
            Snapshot Replication is not configured on the NAS.
          </span>
        </div>
      )}
      <div>
        <h3 className="mb-2 text-xs font-medium text-muted-foreground">
          Btrfs snapshots per share
        </h3>
        {replication.shares.length === 0 ? (
          <p className="text-muted-foreground">No shares found.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Share</th>
                  <th className="py-2 pr-3 font-medium">Snapshots</th>
                </tr>
              </thead>
              <tbody>
                {replication.shares.map((row: ReplicationRow) => (
                  <tr
                    key={row.share}
                    className="border-b border-border/50 hover:bg-accent/20"
                    data-testid={`synology-replication-row-${row.share}`}
                  >
                    <td className="py-2 pr-3">{row.share}</td>
                    <td className="py-2 pr-3">
                      {row.snapshot_count === null ? '—' : row.snapshot_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function ConnectionsPanel({ data }: { data: SynologyConnections }): JSX.Element {
  let body: JSX.Element
  if (!data.data_available) {
    body = (
      <p className="text-muted-foreground" data-testid="synology-connections-unavailable">
        Connection data unavailable — DSM was unreachable
      </p>
    )
  } else if (data.connections.length === 0) {
    body = (
      <p className="text-muted-foreground" data-testid="synology-connections-empty">
        No active connections
      </p>
    )
  } else {
    body = (
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="py-2 pr-3 font-medium">User</th>
              <th className="py-2 pr-3 font-medium">IP</th>
              <th className="py-2 pr-3 font-medium">Type</th>
            </tr>
          </thead>
          <tbody>
            {data.connections.map((row: ConnectionRow, index: number) => (
              <tr
                key={`${row.user}-${row.ip}-${row.type}-${index}`}
                className="border-b border-border/50 hover:bg-accent/20"
              >
                <td className="py-2 pr-3">{row.user}</td>
                <td className="py-2 pr-3">{row.ip}</td>
                <td className="py-2 pr-3">{row.type}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  return (
    <div className="space-y-2 text-sm" data-testid="synology-connections-panel">
      {body}
      <p className="text-xs text-muted-foreground">Live read from DSM — not stored.</p>
    </div>
  )
}

function MountsPanel({
  mountDataAvailable,
  mounts,
}: {
  mountDataAvailable: boolean
  mounts: MountRow[]
}): JSX.Element {
  let body: JSX.Element
  if (!mountDataAvailable) {
    body = (
      <p className="text-muted-foreground" data-testid="synology-mounts-unavailable">
        Mount data unavailable
      </p>
    )
  } else if (mounts.length === 0) {
    body = (
      <p className="text-muted-foreground" data-testid="synology-mounts-empty">
        No NFS mounts
      </p>
    )
  } else {
    body = (
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="py-2 pr-3 font-medium">Mount</th>
              <th className="py-2 pr-3 font-medium">Status</th>
              <th className="py-2 pr-3 font-medium">Free</th>
            </tr>
          </thead>
          <tbody>
            {mounts.map((row: MountRow) => (
              <tr
                key={row.mount}
                className="border-b border-border/50 hover:bg-accent/20"
                data-testid={`synology-mount-row-${row.mount}`}
              >
                <td className="py-2 pr-3">{row.mount}</td>
                <td className="py-2 pr-3">
                  {row.mount_up ? (
                    <Badge variant="ok">Up</Badge>
                  ) : (
                    <Badge variant="critical">Down</Badge>
                  )}
                </td>
                <td className="py-2 pr-3">{formatBytes(row.mount_free_bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  return (
    <div className="space-y-2 text-sm" data-testid="synology-mounts-panel">
      {body}
      <p className="text-xs text-muted-foreground">
        Per-mount latency not exposed by the collector.
      </p>
    </div>
  )
}

export function SynologyOpsTab(): JSX.Element {
  const ops = useSynologyOps()
  const connections = useSynologyConnections()

  const renderConnections = (data: SynologyConnections): JSX.Element => (
    <ConnectionsPanel data={data} />
  )

  const renderOps = (data: SynologyOps): JSX.Element => (
    <div className="space-y-4">
      <PanelSection title="Backup">
        <BackupPanel backup={data.backup} />
      </PanelSection>

      <PanelSection title="Security">
        <SecurityPanel security={data.security} />
      </PanelSection>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="Updates">
          <UpdatesPanel updates={data.updates} />
        </PanelSection>
        <PanelSection title="Replication">
          <ReplicationPanel replication={data.replication} />
        </PanelSection>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="Connections">
          <QueryState
            result={connections}
            unavailableLabel="Synology connection data temporarily unavailable"
            renderData={renderConnections}
          />
        </PanelSection>
        <PanelSection title="Mounts">
          <MountsPanel mountDataAvailable={data.mount_data_available} mounts={data.mounts} />
        </PanelSection>
      </div>
    </div>
  )

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <QueryState
        result={ops}
        unavailableLabel="Synology ops metrics temporarily unavailable"
        renderData={renderOps}
      />
    </div>
  )
}
