import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useSynologyConnections, useSynologyOps } from '@/api/synology'

import { severityVariant } from './synologyFormat'
import { SynologyOpsTab } from './SynologyOpsTab'

type SynologyOps = Schema<'SynologyOps'>
type SynologyConnections = Schema<'SynologyConnections'>

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

function share(over: Partial<Schema<'ReplicationRow'>>): Schema<'ReplicationRow'> {
  return { share: 'share', snapshot_count: 0, ...over }
}

const SHARE_NAMES = [
  'home',
  'photo',
  'video',
  'music',
  'docs',
  'backup',
  'media',
  'archive',
  'projects',
  'downloads',
  'scratch',
  'vault',
  'cctv',
  'misc',
]

const MOCK_OPS: SynologyOps = {
  backup: { configured_count: 3, last_result_ok: true, no_backup_configured: false },
  security: {
    security_safe: false,
    findings: [
      { severity: 'risk', count: 1 },
      { severity: 'danger', count: 0 },
      { severity: 'info', count: null },
    ],
  },
  updates: {
    dsm_update_available: true,
    packages_with_updates_count: 1,
    packages: [
      { package: 'Hyper Backup', update_available: true },
      { package: 'Docker', update_available: false },
    ],
  },
  replication: {
    replication_available: false,
    shares: SHARE_NAMES.map((name) => share({ share: name, snapshot_count: 0 })),
  },
  mount_data_available: true,
  mounts: [{ mount: '/volume1/nfs', mount_up: true, mount_free_bytes: 1073741824 }],
}

const MOCK_CONNECTIONS: SynologyConnections = {
  data_available: true,
  connections: [{ user: 'admin', ip: '192.168.2.10', type: 'CIFS' }],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useSynologyOps).mockReturnValue(
    makeResult<SynologyOps>({ data: MOCK_OPS, isSuccess: true, status: 'success' }),
  )
  vi.mocked(useSynologyConnections).mockReturnValue(
    makeResult<SynologyConnections>({
      data: MOCK_CONNECTIONS,
      isSuccess: true,
      status: 'success',
    }),
  )
})

describe('SynologyOpsTab', () => {
  it('renders all six ops panels', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-backup-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-security-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-updates-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-replication-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-connections-panel')).toBeInTheDocument()
    expect(screen.getByTestId('synology-mounts-panel')).toBeInTheDocument()
  })

  it('renders backup configured count + Last run OK badge', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByText('3 backup tasks configured')).toBeInTheDocument()
    expect(screen.getByText('Last run OK')).toBeInTheDocument()
    expect(
      screen.getByText('Per-job timeline (age/size) not exposed by the DSM collector.'),
    ).toBeInTheDocument()
  })

  it('renders the no-backup-configured warn state', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: {
          ...MOCK_OPS,
          backup: { configured_count: 0, last_result_ok: null, no_backup_configured: true },
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('No backup configured')).toBeInTheDocument()
  })

  it('renders Last run FAILED and No result yet by last_result_ok', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: {
          ...MOCK_OPS,
          backup: { configured_count: 2, last_result_ok: false, no_backup_configured: false },
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('Last run FAILED')).toBeInTheDocument()

    cleanup()
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: {
          ...MOCK_OPS,
          backup: { configured_count: 2, last_result_ok: null, no_backup_configured: false },
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('No result yet')).toBeInTheDocument()
  })

  it('renders security warn + the risk:1 chip + info:— (null) chip + 1 clear caption', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByText('Security advisories present')).toBeInTheDocument()
    // risk:1 (count > 0) shown as normal chip
    expect(screen.getByText('risk: 1')).toBeInTheDocument()
    // info:null shown as muted chip with "—"
    expect(screen.getByText('info: —')).toBeInTheDocument()
    // danger:0 suppressed from chips but counted in caption
    expect(screen.queryByText('danger: 0')).not.toBeInTheDocument()
    expect(screen.getByText('1 other severity clear')).toBeInTheDocument()
  })

  it('renders Secure badge when security_safe is true', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: { ...MOCK_OPS, security: { security_safe: true, findings: [] } },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('Secure')).toBeInTheDocument()
  })

  it('renders the DSM update badge and only update-available packages', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByText('DSM update available')).toBeInTheDocument()
    expect(screen.getByText('1 package updates available')).toBeInTheDocument()
    // Only the update_available package chip is shown
    expect(screen.getByText('Hyper Backup')).toBeInTheDocument()
    expect(screen.queryByText('Docker')).not.toBeInTheDocument()
  })

  it('renders All packages up to date when count is zero', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: {
          ...MOCK_OPS,
          updates: {
            dsm_update_available: false,
            packages_with_updates_count: 0,
            packages: [{ package: 'Docker', update_available: false }],
          },
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('DSM up to date')).toBeInTheDocument()
    expect(screen.getByText('All packages up to date')).toBeInTheDocument()
  })

  it('renders the replication "not enabled" badge and the 14-share table (snapshot 0 -> "0")', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByText('Replication not enabled')).toBeInTheDocument()
    for (const name of SHARE_NAMES) {
      expect(screen.getByTestId(`synology-replication-row-${name}`)).toBeInTheDocument()
    }
    // snapshot_count 0 renders as "0" (not "—"), one per share row
    // Verify: (1) all 14 share names render exactly once each, (2) exactly 14 snapshot "0" cells
    const replicationPanel = screen.getByTestId('synology-replication-panel')
    for (const shareName of SHARE_NAMES) {
      expect(within(replicationPanel).getAllByText(shareName).length).toBe(1)
    }
    // All "0" text within the replication panel are snapshot counts (table has no other "0")
    expect(within(replicationPanel).getAllByText('0').length).toBe(SHARE_NAMES.length)
  })

  it('renders the connections table and the live-read caption', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByText('admin')).toBeInTheDocument()
    expect(screen.getByText('192.168.2.10')).toBeInTheDocument()
    expect(screen.getByText('CIFS')).toBeInTheDocument()
    expect(screen.getByText('Live read from DSM — not stored.')).toBeInTheDocument()
  })

  it('renders the connections empty state', () => {
    vi.mocked(useSynologyConnections).mockReturnValue(
      makeResult<SynologyConnections>({
        data: { data_available: true, connections: [] },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-connections-empty')).toBeInTheDocument()
  })

  it('renders the connections unavailable state', () => {
    vi.mocked(useSynologyConnections).mockReturnValue(
      makeResult<SynologyConnections>({
        data: { data_available: false, connections: [] },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-connections-unavailable')).toBeInTheDocument()
  })

  it('renders the connections 502 unavailable banner independently of ops', () => {
    vi.mocked(useSynologyConnections).mockReturnValue(
      makeResult<SynologyConnections>({
        error: { status: 502 } as ApiError,
        isError: true,
        status: 'error',
      }),
    )
    render(<SynologyOpsTab />)
    // ops panels still render; the connections cell shows the 502 banner text
    expect(screen.getByTestId('synology-backup-panel')).toBeInTheDocument()
    expect(screen.getByText('Synology connection data temporarily unavailable')).toBeInTheDocument()
  })

  it('renders the mounts table when mounts are present', () => {
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-mount-row-/volume1/nfs')).toBeInTheDocument()
    expect(screen.getByText('Up')).toBeInTheDocument()
    expect(screen.getByText('1.0 GiB')).toBeInTheDocument()
  })

  it('renders the "No NFS mounts" empty state', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: { ...MOCK_OPS, mount_data_available: true, mounts: [] },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-mounts-empty')).toBeInTheDocument()
  })

  it('renders the "Mount data unavailable" state', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        data: { ...MOCK_OPS, mount_data_available: false, mounts: [] },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByTestId('synology-mounts-unavailable')).toBeInTheDocument()
  })

  it('renders the ops 502 unavailable banner', () => {
    vi.mocked(useSynologyOps).mockReturnValue(
      makeResult<SynologyOps>({
        error: { status: 502 } as ApiError,
        isError: true,
        status: 'error',
      }),
    )
    render(<SynologyOpsTab />)
    expect(screen.getByText('Synology ops metrics temporarily unavailable')).toBeInTheDocument()
  })

  it('maps severity to badge variant', () => {
    expect(severityVariant('danger')).toBe('critical')
    expect(severityVariant('risk')).toBe('warn')
    expect(severityVariant('warning')).toBe('warn')
    expect(severityVariant('outOfDate')).toBe('warn')
    expect(severityVariant('info')).toBe('muted')
    expect(severityVariant('unknown')).toBe('muted')
    expect(severityVariant('whatever')).toBe('muted')
  })
})
