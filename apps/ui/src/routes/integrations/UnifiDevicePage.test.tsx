import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useUnifiDevice } from '@/api/unifi'

import { UnifiDevicePage } from './UnifiDevicePage'

vi.mock('@/api/unifi')

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({ children }: { children: ReactNode }) => <a>{children}</a>,
    useParams: () => ({ device: 'aa:bb:cc:dd:ee:ff' }),
  }
})

type Detail = Schema<'UnifiDeviceDetail'>

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

const BASE: Detail = {
  mac: 'aa:bb:cc:dd:ee:ff',
  cpu_pct: 10,
  mem_pct: 20,
  load: 0.5,
  ports: [],
  radios: [],
  outlets: [],
  temps: [],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useUnifiDevice).mockReturnValue(ok(BASE))
})

describe('UnifiDevicePage', () => {
  it('renders the back link and system section', () => {
    render(<UnifiDevicePage />)
    expect(screen.getByText(/Back to Unifi overview/i)).toBeInTheDocument()
    expect(screen.getByText('System')).toBeInTheDocument()
  })

  it('shows the empty state when no detail series', () => {
    render(<UnifiDevicePage />)
    expect(screen.getByText(/No detail series available/i)).toBeInTheDocument()
  })

  it('renders a Radios section and maps -1 satisfaction to em-dash', () => {
    vi.mocked(useUnifiDevice).mockReturnValue(
      ok({
        ...BASE,
        radios: [
          {
            radio: 'ng',
            channel: 6,
            num_sta: 3,
            satisfaction: -1,
            bandwidth_mhz: null,
            cu_self_rx: null,
            cu_self_tx: null,
            cu_total: null,
            tx_power: null,
            tx_retries_pct: null,
          },
        ],
      }),
    )
    render(<UnifiDevicePage />)
    expect(screen.getByText('Radios')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('renders a Ports section when ports present', () => {
    vi.mocked(useUnifiDevice).mockReturnValue(
      ok({
        ...BASE,
        ports: [
          {
            port_idx: '1',
            up: true,
            speed_bps: 1000000000,
            satisfaction: 95,
            link_down_count: null,
            mac_table_count: null,
            poe_current_ma: null,
            poe_good: null,
            poe_power_watts: null,
            poe_voltage: null,
            rx_bytes: null,
            rx_dropped: null,
            rx_errors: null,
            tx_bytes: null,
            tx_dropped: null,
            tx_errors: null,
          },
        ],
      }),
    )
    render(<UnifiDevicePage />)
    expect(screen.getByText('Ports')).toBeInTheDocument()
    expect(screen.getByText('95%')).toBeInTheDocument()
  })

  it('renders temps list when device has temperature sensors', () => {
    vi.mocked(useUnifiDevice).mockReturnValue(
      ok({
        ...BASE,
        temps: [{ name: 'CPU', value: 55 }],
      }),
    )
    render(<UnifiDevicePage />)
    // The temps map renders Object.entries as "key: value" spans
    expect(screen.getByText(/name: CPU/i)).toBeInTheDocument()
  })

  it('renders outlets list with On/Off state', () => {
    vi.mocked(useUnifiDevice).mockReturnValue(
      ok({
        ...BASE,
        outlets: [
          { outlet: '1', name: 'Server', relay_state: true },
          { outlet: '2', name: 'NAS', relay_state: false },
        ],
      }),
    )
    render(<UnifiDevicePage />)
    expect(screen.getByText('Outlets')).toBeInTheDocument()
    expect(screen.getByText('On')).toBeInTheDocument()
    expect(screen.getByText('Off')).toBeInTheDocument()
  })
})
