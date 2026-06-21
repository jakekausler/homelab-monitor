import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useUnifiClients } from '@/api/unifi'

import { NetworkClientsTab } from './NetworkClientsTab'

vi.mock('@/api/unifi')
vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({ children, ...rest }: { children: ReactNode; [key: string]: unknown }) => (
      <a {...rest}>{children}</a>
    ),
  }
})

type Resp = Schema<'UnifiClientsResponse'>
type Row = Schema<'UnifiClientRowModel'>

function row(over: Partial<Row>): Row {
  return {
    ap_mac: null,
    hostname: null,
    ip: null,
    is_host: false,
    last_seen: '2026-06-20T00:00:00Z',
    lease_expiry: null,
    mac: 'aa:aa:aa:aa:aa:aa',
    name: null,
    network: 'LAN',
    online: true,
    use_fixedip: false,
    ...over,
  }
}

function ok(data: Resp): UseQueryResult<Resp, ApiError> {
  return {
    data,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: true,
    status: 'success',
  } as UseQueryResult<Resp, ApiError>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const HOST = row({ mac: 'host:mac', name: 'monitor-host', is_host: true })
const WIFI = row({ mac: 'wifi:client', name: 'Phone', ap_mac: 'ap:11:22', ip: '192.168.1.10' })
const WIRED = row({ mac: 'wired:client', name: 'Desktop', ap_mac: null, ip: '192.168.1.20' })

beforeEach(() => {
  vi.mocked(useUnifiClients).mockReturnValue(
    ok({ clients: [WIFI, HOST, WIRED], limit: 500, offset: 0, total: 3 }),
  )
})

describe('NetworkClientsTab', () => {
  it('renders client rows', () => {
    render(<NetworkClientsTab />)
    expect(screen.getByText('Phone')).toBeInTheDocument()
    expect(screen.getByText('Desktop')).toBeInTheDocument()
  })

  it('pins the host row with a Host badge', () => {
    render(<NetworkClientsTab />)
    expect(screen.getByText('monitor-host')).toBeInTheDocument()
    expect(screen.getByText('Host')).toBeInTheDocument()
  })

  it('keeps the host visible even when search excludes it (filter-exempt)', () => {
    render(<NetworkClientsTab />)
    fireEvent.change(screen.getByTestId('clients-search'), { target: { value: 'Phone' } })
    expect(screen.getByText('Phone')).toBeInTheDocument()
    expect(screen.queryByText('Desktop')).not.toBeInTheDocument()
    expect(screen.getByText('monitor-host')).toBeInTheDocument() // host exempt
  })

  it('shows Wi-Fi vs Wired in the Connection column', () => {
    render(<NetworkClientsTab />)
    expect(screen.getByText('Wi-Fi (ap:11:22)')).toBeInTheDocument()
    expect(screen.getAllByText('Wired')).toHaveLength(2)
  })

  it('sorts when a header is clicked', () => {
    render(<NetworkClientsTab />)
    // default name asc → Desktop before Phone among non-host; click toggles to desc
    fireEvent.click(screen.getByTestId('clients-sort-name'))
    // assertion: after desc, Phone's row precedes Desktop's. Use getAllByTestId on the
    // client link testids and assert ordering of the non-host links.
    const links = screen.getAllByTestId(/^client-link-(wifi|wired):client$/)
    expect(links[0]).toHaveAttribute('data-testid', 'client-link-wifi:client')
  })

  it('renders the empty state when no clients', () => {
    vi.mocked(useUnifiClients).mockReturnValue(ok({ clients: [], limit: 500, offset: 0, total: 0 }))
    render(<NetworkClientsTab />)
    expect(screen.getByText(/No clients found/i)).toBeInTheDocument()
  })
})
