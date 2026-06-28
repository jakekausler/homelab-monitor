import type { JSX } from 'react'
import { useState } from 'react'

import { useSynologyHardware } from '@/api/synology'
import type { Schema } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { formatPct, formatTemp } from './unifiFormat'
import { formatDuration, formatUptime } from '@/lib/relativeTime'

import { PanelSection } from './PanelSection'
import { QueryState } from './QueryState'
import { SynologyDiskSmartDialog } from './SynologyDiskSmartDialog'
import {
  diskStatusBadge,
  formatRemainLife,
  smartFailingBadge,
  statusTone,
  tempVariant,
  volumeVariant,
} from './synologyFormat'
import type { DiskRow, FanRow, PoolRow, VolumeRow } from './synologyFormat'

type SynologyHardware = Schema<'SynologyHardware'>
type SynologySystem = Schema<'SynologySystem'>
type SynologyUps = Schema<'SynologyUps'>
type SynologySshProbe = Schema<'SynologySshProbe'>

function SystemPanel({ system }: { system: SynologySystem }): JSX.Element {
  const tempBadge = tempVariant(system.sys_temp_celsius)
  const healthBadge = system.health_ok ? (
    <Badge variant="ok">Healthy</Badge>
  ) : (
    <Badge variant="warn">Degraded</Badge>
  )
  const rebootBadge = system.need_reboot ? <Badge variant="warn">Reboot needed</Badge> : null
  return (
    <div className="space-y-2 text-sm" data-testid="synology-system-panel">
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
        <div>
          <span className="text-muted-foreground">Model: </span>
          {system.model ?? '—'}
        </div>
        <div>
          <span className="text-muted-foreground">Serial: </span>
          {system.serial ?? '—'}
        </div>
        <div>
          <span className="text-muted-foreground">Firmware: </span>
          {system.firmware ?? '—'}
        </div>
        <div>
          <span className="text-muted-foreground">Uptime: </span>
          {formatUptime(system.uptime_seconds)}
        </div>
        <div>
          <span className="text-muted-foreground">Temp: </span>
          <Badge variant={tempBadge}>{formatTemp(system.sys_temp_celsius)}</Badge>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Health: </span>
          {healthBadge}
          {rebootBadge}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">Fans: </span>
        {system.fans.length === 0 ? (
          <span className="text-muted-foreground">—</span>
        ) : (
          system.fans.map((fan: FanRow, index: number) => (
            <Badge key={`${fan.state}-${index}`} variant="muted">
              {fan.state}: {formatPct(fan.value)}
            </Badge>
          ))
        )}
      </div>
    </div>
  )
}

function VolumesPanel({ volumes }: { volumes: VolumeRow[] }): JSX.Element {
  if (volumes.length === 0) {
    return <p className="text-sm text-muted-foreground">No volumes found.</p>
  }
  return (
    <div className="space-y-2 text-sm" data-testid="synology-volumes-panel">
      {volumes.map((vol) => (
        <div key={vol.volume} className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate">{vol.volume}</span>
          <div className="min-w-0 flex items-center gap-2 overflow-x-auto whitespace-nowrap">
            <Badge variant={volumeVariant(vol.used_percent)}>{formatPct(vol.used_percent)}</Badge>
            {vol.status.length === 0 ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              vol.status.map((s) => (
                <Badge key={s} variant={statusTone(s)}>
                  {s}
                </Badge>
              ))
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function PoolsPanel({ pools }: { pools: PoolRow[] }): JSX.Element {
  if (pools.length === 0) {
    return <p className="text-sm text-muted-foreground">No pools found.</p>
  }
  return (
    <div className="space-y-2 text-sm" data-testid="synology-pools-panel">
      {pools.map((pool) => (
        <div key={pool.pool} className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate">{pool.pool}</span>
          <div className="min-w-0 flex items-center gap-2 overflow-x-auto whitespace-nowrap">
            {pool.status.length === 0 ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              pool.status.map((s) => (
                <Badge key={s} variant={statusTone(s)}>
                  {s}
                </Badge>
              ))
            )}
            <span className="text-muted-foreground">{pool.raid_status || '—'}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

function UpsPanel({ ups }: { ups: SynologyUps }): JSX.Element {
  const connectedBadge = ups.connected ? (
    <Badge variant="ok">Connected</Badge>
  ) : (
    <Badge variant="critical">Disconnected</Badge>
  )
  const batteryBadge = ups.on_battery ? (
    <Badge variant="warn">On battery</Badge>
  ) : (
    <Badge variant="ok">On mains</Badge>
  )
  const charge = ups.charge_percent
  const chargeVariant = charge === null || charge >= 100 ? 'ok' : 'warn'
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="synology-ups-panel">
      {connectedBadge}
      {batteryBadge}
      <Badge variant={chargeVariant}>Charge: {formatPct(charge)}</Badge>
    </div>
  )
}

function SshProbePanel({
  probe,
  dataAvailable,
}: {
  probe: SynologySshProbe
  dataAvailable: boolean
}): JSX.Element {
  const upBadge = probe.up ? <Badge variant="ok">Up</Badge> : <Badge variant="critical">Down</Badge>
  const mismatchBadge = probe.host_key_mismatch ? (
    <Badge variant="critical">Host-key mismatch</Badge>
  ) : null
  const degradedBadge = probe.mdstat_array_degraded ? (
    <Badge variant="critical">Array degraded</Badge>
  ) : (
    <Badge variant="ok">Array OK</Badge>
  )
  const lastSuccess =
    probe.last_success_age_seconds === null
      ? 'never succeeded'
      : formatDuration(probe.last_success_age_seconds)

  const showDownNote = dataAvailable && !probe.up
  const showNoDataNote = !dataAvailable

  return (
    <div className="space-y-2 text-sm" data-testid="synology-ssh-probe-panel">
      <div className="flex flex-wrap items-center gap-2">
        {upBadge}
        {mismatchBadge}
        {degradedBadge}
      </div>
      <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
        <div>
          <span className="text-muted-foreground">Load1: </span>
          {probe.load1 === null ? '—' : probe.load1.toFixed(2)}
        </div>
        <div>
          <span className="text-muted-foreground">CPU temp: </span>
          {formatTemp(probe.cpu_temp_celsius)}
        </div>
        <div>
          <span className="text-muted-foreground">Last success: </span>
          {lastSuccess}
        </div>
        <div>
          <span className="text-muted-foreground">Probe duration: </span>
          {formatDuration(probe.probe_duration_seconds)}
        </div>
      </div>
      {showDownNote && (
        <div
          className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800"
          role="status"
          data-testid="synology-ssh-probe-down"
        >
          SSH probe connection down — load, CPU temp, and per-disk SMART details unavailable. Last
          collector run succeeded; SSH connection to the NAS is failing.
        </div>
      )}
      {showNoDataNote && (
        <div
          className="rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground"
          data-testid="synology-ssh-probe-nodata"
        >
          No SSH probe data collected.
        </div>
      )}
    </div>
  )
}

function DisksTable({
  disks,
  onDrill,
}: {
  disks: DiskRow[]
  onDrill: (disk: string) => void
}): JSX.Element {
  if (disks.length === 0) {
    return <p className="text-sm text-muted-foreground">No disks found.</p>
  }
  const sorted = [...disks].sort((a, b) => a.disk.localeCompare(b.disk))
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="py-2 pr-3 font-medium">Disk</th>
            <th className="py-2 pr-3 font-medium">Model</th>
            <th className="py-2 pr-3 font-medium">Status</th>
            <th className="py-2 pr-3 font-medium">SMART</th>
            <th className="py-2 pr-3 font-medium">Temp</th>
            <th className="py-2 pr-3 font-medium">Life</th>
            <th className="py-2 pr-3 font-medium">Attrs</th>
            <th className="py-2 pr-3 font-medium" />
          </tr>
        </thead>
        <tbody>
          {sorted.map((disk) => {
            const status = diskStatusBadge(disk.status)
            const smart = diskStatusBadge(disk.smart_status)
            const failing = smartFailingBadge(disk.smart_attr_failing)
            return (
              <tr
                key={disk.disk}
                className="border-b border-border/50 hover:bg-accent/20"
                data-testid={`synology-disk-row-${disk.disk}`}
              >
                <td className="py-2 pr-3">{disk.disk}</td>
                <td className="py-2 pr-3 text-muted-foreground">{disk.model}</td>
                <td className="py-2 pr-3">
                  <Badge variant={status.variant}>{status.label}</Badge>
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={smart.variant}>{smart.label}</Badge>
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={tempVariant(disk.temp_celsius)}>
                    {formatTemp(disk.temp_celsius)}
                  </Badge>
                </td>
                <td className="py-2 pr-3 text-muted-foreground">
                  {formatRemainLife(disk.remain_life)}
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={failing.variant}>{failing.label}</Badge>
                </td>
                <td className="py-2 pr-3">
                  <button
                    type="button"
                    className="text-xs text-foreground underline hover:no-underline"
                    data-testid={`synology-disk-drill-${disk.disk}`}
                    onClick={() => onDrill(disk.disk)}
                  >
                    SMART…
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export function SynologyHardwareTab(): JSX.Element {
  const hardware = useSynologyHardware()
  const [selectedDisk, setSelectedDisk] = useState('')

  const renderData = (data: SynologyHardware): JSX.Element => (
    <div className="space-y-4">
      <PanelSection title="System">
        <SystemPanel system={data.system} />
      </PanelSection>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="Volumes">
          <VolumesPanel volumes={data.volumes} />
        </PanelSection>
        <PanelSection title="RAID / Pool">
          <PoolsPanel pools={data.pools} />
        </PanelSection>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <PanelSection title="UPS">
          <UpsPanel ups={data.ups} />
        </PanelSection>
        <PanelSection title="SSH probe">
          <SshProbePanel probe={data.ssh_probe} dataAvailable={data.ssh_probe_data_available} />
        </PanelSection>
      </div>

      <PanelSection title="Disks (SMART)">
        <DisksTable disks={data.disks} onDrill={setSelectedDisk} />
      </PanelSection>
    </div>
  )

  return (
    <div className="h-full space-y-4 overflow-y-auto p-4">
      <QueryState
        result={hardware}
        unavailableLabel="Synology hardware metrics temporarily unavailable"
        renderData={renderData}
      />
      <SynologyDiskSmartDialog
        disk={selectedDisk}
        open={selectedDisk.length > 0}
        onOpenChange={(open) => {
          if (!open) setSelectedDisk('')
        }}
      />
    </div>
  )
}
