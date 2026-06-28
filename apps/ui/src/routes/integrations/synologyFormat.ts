import type { Schema } from '@/api/types'

type BadgeVariant = 'ok' | 'warn' | 'critical' | 'muted'

/** Temperature severity: green <45, amber 45–55, red >55. null -> muted. */
export function tempVariant(value: number | null | undefined): BadgeVariant {
  if (value === null || value === undefined) return 'muted'
  if (value > 55) return 'critical'
  if (value >= 45) return 'warn'
  return 'ok'
}

/** Volume used-percent severity: <80 ok, 80–90 warn, >90 critical. null -> muted. */
export function volumeVariant(value: number | null | undefined): BadgeVariant {
  if (value === null || value === undefined) return 'muted'
  if (value > 90) return 'critical'
  if (value >= 80) return 'warn'
  return 'ok'
}

/** Disk status / smart_status float: 1.0 -> ok "Healthy", null -> muted "Unknown", else critical "Fault". */
export function diskStatusBadge(value: number | null | undefined): {
  variant: BadgeVariant
  label: string
} {
  if (value === null || value === undefined) return { variant: 'muted', label: 'Unknown' }
  if (value === 1) return { variant: 'ok', label: 'Healthy' }
  return { variant: 'critical', label: 'Fault' }
}

/** SMART attr-failing flag: true -> critical "Failing", false -> ok "OK". */
export function smartFailingBadge(failing: boolean): { variant: BadgeVariant; label: string } {
  return failing ? { variant: 'critical', label: 'Failing' } : { variant: 'ok', label: 'OK' }
}

/**
 * remain_life rendering: -1 -> "N/A" (HDD reports no SSD wear), null -> "—",
 * else the integer percent.
 */
export function formatRemainLife(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value < 0) return 'N/A'
  return `${Math.round(value)}%`
}

/**
 * Map a Synology status string to a badge tone. Known-good -> ok, known-warn
 * -> warn, known-critical -> critical, anything else -> warn (the raw text is shown alongside by the caller).
 */
export function statusTone(status: string | null | undefined): BadgeVariant {
  if (status === null || status === undefined || status === '') return 'muted'
  const normal = new Set(['normal', 'pool_normal'])
  const warn = new Set(['has_unverified_disk', 'fs_almost_full'])
  const critical = new Set(['degraded', 'crashed', 'crashing', 'read_only', 'failing', 'error'])
  if (normal.has(status)) return 'ok'
  if (critical.has(status)) return 'critical'
  if (warn.has(status)) return 'warn'
  return 'warn'
}

/**
 * Map a Synology security-finding severity to a badge tone.
 * danger -> critical; risk/warning/outOfDate -> warn; everything else -> muted.
 */
export function severityVariant(severity: string): BadgeVariant {
  switch (severity) {
    case 'danger':
      return 'critical'
    case 'risk':
    case 'warning':
    case 'outOfDate':
      return 'warn'
    default:
      return 'muted'
  }
}

/** Type alias re-exports for the tab (keeps row prop typing local). */
export type DiskRow = Schema<'DiskRow'>
export type SmartAttrRow = Schema<'SmartAttrRow'>
export type VolumeRow = Schema<'VolumeRow'>
export type PoolRow = Schema<'PoolRow'>
export type FanRow = Schema<'FanRow'>

export type SynologyBackup = Schema<'SynologyBackup'>
export type SynologyReplication = Schema<'SynologyReplication'>
export type ReplicationRow = Schema<'ReplicationRow'>
export type SynologyUpdates = Schema<'SynologyUpdates'>
export type PackageUpdateRow = Schema<'PackageUpdateRow'>
export type SynologySecurity = Schema<'SynologySecurity'>
export type SecurityFindingRow = Schema<'SecurityFindingRow'>
export type MountRow = Schema<'MountRow'>
export type ConnectionRow = Schema<'ConnectionRow'>
