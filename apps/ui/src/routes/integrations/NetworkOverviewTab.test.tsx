import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useUnifiDhcp, useUnifiDnsPosture, useUnifiWan, useUnifiWifi } from '@/api/unifi'

import { NetworkOverviewTab } from './NetworkOverviewTab'

vi.mock('@/api/unifi')

// Stub the chart — recharts ResponsiveContainer is 0×0 in jsdom; the chart has
// its own dedicated test. Here we only assert the tab wires data + widgets.
vi.mock('@/components/charts/UnifiRangeChart', () => ({
  UnifiRangeChart: ({ title }: { title: string }) => (
    <div data-testid="range-chart-stub">{title}</div>
  ),
}))

function ok<T>(data: T): UseQueryResult<T, ApiError> {
  return {
    data,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: true,
    status: 'success',
  } as UseQueryResult<T, ApiError>
}

const WAN: Schema<'UnifiWanCurrent'> = {
  download_mbps: 942.5,
  upload_mbps: 35.1,
  latency_seconds: 0.012,
  ping_seconds: 0.011,
  speedtest_lastrun: 1_700_000_000,
  wan_up: true,
  wan_uptime_seconds: 90000,
  failover_capable: false,
  failover_active: false,
  xput_down_mbps: 880.0,
  xput_up_mbps: 30.0,
}

const DHCP_DISABLED: Schema<'UnifiNetworkDhcpResponse'> = {
  networks: [
    {
      network: 'Default',
      dhcp_enabled: false,
      pool_start: null,
      pool_end: null,
      pool_size: null,
      occupancy: null,
      reservation_count: 11,
    },
  ],
}

const WIFI: Schema<'UnifiWifiResponse'> = {
  poor_signal: 2,
  poor_satisfaction: 1,
  high_retries: 0,
  by_band: [{ key: '5ghz', count: 7 }],
  by_link: [{ key: 'wired', count: 4 }],
  ssids: [{ ssid: 'HomeNet', count: 9 }],
}

const DNS_EMPTY: Schema<'UnifiDnsPostureResponse'> = { networks: [] }

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useUnifiWan).mockReturnValue(ok(WAN))
  vi.mocked(useUnifiDhcp).mockReturnValue(ok(DHCP_DISABLED))
  vi.mocked(useUnifiWifi).mockReturnValue(ok(WIFI))
  vi.mocked(useUnifiDnsPosture).mockReturnValue(ok(DNS_EMPTY))
})

describe('NetworkOverviewTab', () => {
  it('renders WAN current values', () => {
    render(<NetworkOverviewTab />)
    expect(screen.getByText('WAN up')).toBeInTheDocument()
    expect(screen.getByText('942.5 Mbps')).toBeInTheDocument()
    expect(screen.getByText('35.1 Mbps')).toBeInTheDocument()
    expect(screen.getByText('12 ms')).toBeInTheDocument() // latency_seconds 0.012 -> 12 ms
  })

  it('renders both range chart stubs', () => {
    render(<NetworkOverviewTab />)
    expect(screen.getByText('Speedtest (Mbps)')).toBeInTheDocument()
    expect(screen.getByText('WAN latency')).toBeInTheDocument()
  })

  it('renders DHCP-disabled honest state', () => {
    render(<NetworkOverviewTab />)
    expect(screen.getByText(/DHCP not enabled on this network/i)).toBeInTheDocument()
  })

  it('renders DHCP pool details when enabled', () => {
    vi.mocked(useUnifiDhcp).mockReturnValue(
      ok({
        networks: [
          {
            network: 'IoT',
            dhcp_enabled: true,
            pool_start: '10.0.5.10',
            pool_end: '10.0.5.250',
            pool_size: 240,
            occupancy: 0.42,
            reservation_count: 3,
          },
        ],
      }),
    )
    render(<NetworkOverviewTab />)
    expect(screen.getByText(/10\.0\.5\.10/)).toBeInTheDocument()
    expect(screen.getByText(/Reservations: 3/)).toBeInTheDocument()
    expect(screen.getByText(/Occupancy: 42%/)).toBeInTheDocument()
  })

  it('renders WiFi experience counts and SSID distribution', () => {
    render(<NetworkOverviewTab />)
    expect(screen.getByText('5 GHz')).toBeInTheDocument()
    expect(screen.getByText('HomeNet')).toBeInTheDocument()
  })

  it('renders the honest DNS-posture empty state', () => {
    render(<NetworkOverviewTab />)
    expect(screen.getByText(/No per-network DNS overrides configured/i)).toBeInTheDocument()
  })

  it('renders DNS posture rows when present', () => {
    vi.mocked(useUnifiDnsPosture).mockReturnValue(
      ok({ networks: [{ network: 'Guest', dns: '1.1.1.1' }] }),
    )
    render(<NetworkOverviewTab />)
    expect(screen.getByText('Guest')).toBeInTheDocument()
    expect(screen.getByText('1.1.1.1')).toBeInTheDocument()
  })

  it('shows a 502 unavailable banner for WAN', () => {
    const err = new Error('bad gateway') as ApiError
    ;(err as { status: number }).status = 502
    vi.mocked(useUnifiWan).mockReturnValue({
      data: undefined,
      error: err,
      isPending: false,
      isError: true,
      isSuccess: false,
      status: 'error',
    } as UseQueryResult<Schema<'UnifiWanCurrent'>, ApiError>)
    render(<NetworkOverviewTab />)
    expect(screen.getByText('WAN data temporarily unavailable')).toBeInTheDocument()
  })
})
