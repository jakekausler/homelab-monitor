import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useSynologyDiskSmart, useSynologyHardware } from '@/api/synology'

import { SynologyHardwareTab } from './SynologyHardwareTab'

type SynologyHardware = Schema<'SynologyHardware'>
type SynologyDiskSmartAttrs = Schema<'SynologyDiskSmartAttrs'>

vi.mock('@/api/synology')

function makeResult<T>(
  overrides: Partial<UseQueryResult<T, ApiError>>,
): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    status: 'pending',
    ...overrides,
  } as UseQueryResult<T, ApiError>
}

function disk(over: Partial<Schema<'DiskRow'>>): Schema<'DiskRow'> {
  return {
    disk: 'sda',
    model: 'WD40EFRX',
    status: 1,
    smart_status: 1,
    temp_celsius: 38,
    smart_attr_failing: false,
    remain_life: -1,
    ...over,
  }
}

const MOCK_HARDWARE: SynologyHardware = {
  volumes: [{ volume: 'volume_1', used_percent: 73.4, status: ['normal'] }],
  pools: [{ pool: 'pool_1', status: ['normal'], raid_status: 'raid_5' }],
  disks: [
    disk({ disk: 'sda' }),
    disk({ disk: 'sdb' }),
    disk({ disk: 'sdc' }),
    disk({ disk: 'sdd' }),
    disk({ disk: 'sde' }),
    disk({ disk: 'sdf' }),
    disk({ disk: 'sdg' }),
    disk({ disk: 'sdh' }),
  ],
  system: {
    health_ok: true,
    uptime_seconds: 123456,
    sys_temp_celsius: 41,
    need_reboot: false,
    model: 'DS920+',
    serial: 'ABC123',
    firmware: 'DSM 7.2',
    fans: [{ state: 'normal', value: 60 }],
  },
  ups: { connected: true, on_battery: false, charge_percent: 100 },
  ssh_probe: {
    load1: 0.42,
    cpu_temp_celsius: 45,
    mdstat_array_degraded: false,
    up: true,
    host_key_mismatch: false,
    last_success_age_seconds: 12.5,
    probe_duration_seconds: 0.8,
  },
  ssh_probe_data_available: true,
}

const SMART_ATTRS: SynologyDiskSmartAttrs = {
  disk: 'sda',
  attrs: [{ attr_id: '5', attr_name: 'Reallocated', raw: 0, worst: 100, threshold: 10 }],
  data_available: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useSynologyHardware).mockReturnValue(
    makeResult<SynologyHardware>({ data: MOCK_HARDWARE, isSuccess: true, status: 'success' }),
  )
  vi.mocked(useSynologyDiskSmart).mockReturnValue(
    makeResult<SynologyDiskSmartAttrs>({
      data: SMART_ATTRS,
      isSuccess: true,
      status: 'success',
    }),
  )
})

describe('SynologyHardwareTab', () => {
  it('renders all widget sections from hardware data', () => {
    render(<SynologyHardwareTab />)
    expect(screen.getByTestId('synology-system-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-volumes-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-pools-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-ups-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-ssh-probe-panel')).toBeInTheDocument()
    expect(screen.getByText('DS920+')).toBeInTheDocument()
  })

  it('renders all 8 disk rows with model and remain_life N/A', () => {
    render(<SynologyHardwareTab />)
    for (const id of ['sda', 'sdb', 'sdc', 'sdd', 'sde', 'sdf', 'sdg', 'sdh']) {
      expect(screen.getByTestId(`synology-disk-row-${id}`)).toBeInTheDocument()
    }
    // remain_life -1 -> "N/A" (one per disk row)
    expect(screen.getAllByText('N/A').length).toBe(8)
    // model label present
    expect(screen.getAllByText('WD40EFRX').length).toBe(8)
  })

  it('renders honest "connection down" note when probe up=false but data_available', () => {
    vi.mocked(useSynologyHardware).mockReturnValue(
      makeResult<SynologyHardware>({
        data: {
          ...MOCK_HARDWARE,
          ssh_probe: { ...MOCK_HARDWARE.ssh_probe, up: false },
          ssh_probe_data_available: true,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    expect(screen.getByTestId('synology-ssh-probe-down')).toBeInTheDocument()
    expect(screen.queryByTestId('synology-ssh-probe-nodata')).not.toBeInTheDocument()
  })

  it('renders "no SSH probe data" note when data_available is false', () => {
    vi.mocked(useSynologyHardware).mockReturnValue(
      makeResult<SynologyHardware>({
        data: { ...MOCK_HARDWARE, ssh_probe_data_available: false },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    expect(screen.getByTestId('synology-ssh-probe-nodata')).toBeInTheDocument()
  })

  it('applies critical thresholds for high volume usage and high temp', () => {
    vi.mocked(useSynologyHardware).mockReturnValue(
      makeResult<SynologyHardware>({
        data: {
          ...MOCK_HARDWARE,
          volumes: [{ volume: 'volume_1', used_percent: 95, status: ['normal'] }],
          system: { ...MOCK_HARDWARE.system, sys_temp_celsius: 60 },
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    // 95% volume -> critical badge text "95%"
    const volBadge = screen.getByText('95%')
    expect(volBadge.className).toContain('red')
    // 60C system temp -> red badge "60°C"
    const tempBadge = screen.getByText('60°C')
    expect(tempBadge.className).toContain('red')
  })

  it('renders one row per volume/pool with multiple status badges when status list has multiple entries', () => {
    vi.mocked(useSynologyHardware).mockReturnValue(
      makeResult<SynologyHardware>({
        data: {
          ...MOCK_HARDWARE,
          volumes: [
            {
              volume: 'volume_1',
              used_percent: 73.4,
              status: ['fs_almost_full', 'has_unverified_disk'],
            },
          ],
          pools: [
            {
              pool: 'pool_1',
              status: ['pool_normal', 'has_unverified_disk'],
              raid_status: 'raid_5',
            },
          ],
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    // Only one volume row (no duplication)
    expect(screen.getAllByText('volume_1').length).toBe(1)
    // Both status badges render on volume
    expect(screen.getByText('fs_almost_full')).toBeInTheDocument()
    // has_unverified_disk appears twice: once on volume, once on pool
    expect(screen.getAllByText('has_unverified_disk').length).toBe(2)
    // Only one pool row (no duplication)
    expect(screen.getAllByText('pool_1').length).toBe(1)
    expect(screen.getByText('pool_normal')).toBeInTheDocument()
  })

  it('renders muted dash when volume/pool status list is empty', () => {
    vi.mocked(useSynologyHardware).mockReturnValue(
      makeResult<SynologyHardware>({
        data: {
          ...MOCK_HARDWARE,
          volumes: [{ volume: 'volume_1', used_percent: 73.4, status: [] }],
          pools: [{ pool: 'pool_1', status: [], raid_status: 'raid_5' }],
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    // "—" for empty status lists (2 of them — one volumes, one pools)
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })

  it('opens the SMART drill-down dialog and shows the attribute table', () => {
    render(<SynologyHardwareTab />)
    fireEvent.click(screen.getByTestId('synology-disk-drill-sda'))
    expect(screen.getByTestId('synology-smart-dialog')).toBeInTheDocument()
    expect(screen.getByTestId('synology-smart-attr-5')).toBeInTheDocument()
    expect(screen.getByText('Reallocated')).toBeInTheDocument()
  })

  it('shows the drill honest-empty state when probe is down (attrs empty, data_available)', () => {
    vi.mocked(useSynologyDiskSmart).mockReturnValue(
      makeResult<SynologyDiskSmartAttrs>({
        data: { disk: 'sda', attrs: [], data_available: true },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyHardwareTab />)
    fireEvent.click(screen.getByTestId('synology-disk-drill-sda'))
    expect(screen.getByTestId('synology-smart-empty-down')).toBeInTheDocument()
  })
})
