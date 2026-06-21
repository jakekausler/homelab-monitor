import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useUnifiClient } from '@/api/unifi'

import { NetworkClientPage } from './NetworkClientPage'

vi.mock('@/api/unifi')
vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({ children }: { children: ReactNode }) => <a>{children}</a>,
    useParams: () => ({ mac: 'aa:bb:cc:dd:ee:ff' }),
  }
})

type Detail = Schema<'UnifiClientDetail'>

function ok(data: Detail): UseQueryResult<Detail, ApiError> {
  return {
    data,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: true,
    status: 'success',
  } as UseQueryResult<Detail, ApiError>
}

function err(status: number): UseQueryResult<Detail, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<Detail, ApiError>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const BASE: Detail = {
  mac: 'aa:bb:cc:dd:ee:ff',
  name: 'Wired Box',
  hostname: null,
  ip: '192.168.1.50',
  network: 'LAN',
  is_host: false,
  online: true,
  ap_mac: null,
  sw_mac: 'sw:1',
  sw_port: 5,
  oui: 'Acme Corp',
  first_seen: '2026-06-10T00:00:00Z',
  last_seen: '2026-06-20T00:00:00Z',
  lease_expiry: '2026-06-30T00:00:00Z',
  fixed_ip: null,
  use_fixedip: false,
  dns: null,
  dpi: [
    { app: 'HTTP', cat: 'Web', bytes: 10 },
    { app: 'DNS', cat: 'Network', bytes: 99 },
  ],
  series: { signal_dbm: null, tx_rate_bps: null, rx_rate_bps: null },
}

describe('NetworkClientPage', () => {
  beforeEach(() => {
    vi.mocked(useUnifiClient).mockReturnValue(ok(BASE))
  })

  it('renders identity + back link', () => {
    render(<NetworkClientPage />)
    expect(screen.getByText(/Back to clients/i)).toBeInTheDocument()
    expect(screen.getByText('Identity')).toBeInTheDocument()
    expect(screen.getByText('Wired Box')).toBeInTheDocument()
  })

  it('renders connection for wifi client', () => {
    const wifi: Detail = {
      ...BASE,
      ap_mac: 'ap:11:22:33:44:55',
      series: { signal_dbm: -58, tx_rate_bps: 50_000_000, rx_rate_bps: 75_000_000 },
    }
    vi.mocked(useUnifiClient).mockReturnValue(ok(wifi))
    render(<NetworkClientPage />)
    expect(screen.getByText('-58 dBm')).toBeInTheDocument()
    expect(screen.getByText(/Wi-Fi via ap:11:22:33:44:55/)).toBeInTheDocument()
  })

  it('wired client shows "—" for signal and rates', () => {
    render(<NetworkClientPage />)
    expect(screen.getByText(/Switch sw:1 port 5/)).toBeInTheDocument()
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(2) // at minimum: signal, TX, RX
  })

  it('renders DPI sorted by bytes desc', () => {
    render(<NetworkClientPage />)
    const rows = screen.getAllByRole('row')
    // Skip header row (index 0), check data rows
    const dataRows = rows.slice(1)
    expect(dataRows[0]?.textContent).toContain('DNS') // DNS first (99 bytes)
    expect(dataRows[1]?.textContent).toContain('HTTP') // HTTP second (10 bytes)
  })

  it('renders empty DPI state', () => {
    const noDpi: Detail = { ...BASE, dpi: [] }
    vi.mocked(useUnifiClient).mockReturnValue(ok(noDpi))
    render(<NetworkClientPage />)
    expect(screen.getByText(/No DPI data/i)).toBeInTheDocument()
  })

  it('renders DNS placeholder when dns null', () => {
    render(<NetworkClientPage />)
    expect(screen.getByText(/provided by Pi-hole/i)).toBeInTheDocument()
  })

  it('renders Host badge when is_host', () => {
    const host: Detail = { ...BASE, is_host: true, name: 'monitor-host' }
    vi.mocked(useUnifiClient).mockReturnValue(ok(host))
    render(<NetworkClientPage />)
    expect(screen.getByText('Host')).toBeInTheDocument()
  })

  it('renders not-found state on 404', () => {
    vi.mocked(useUnifiClient).mockReturnValue(err(404))
    render(<NetworkClientPage />)
    expect(screen.getByText('Client not found')).toBeInTheDocument()
    expect(screen.queryByText('Identity')).not.toBeInTheDocument()
  })

  it('renders unavailable on 502', () => {
    vi.mocked(useUnifiClient).mockReturnValue(err(502))
    render(<NetworkClientPage />)
    expect(screen.getByText('Client data temporarily unavailable')).toBeInTheDocument()
  })
})
