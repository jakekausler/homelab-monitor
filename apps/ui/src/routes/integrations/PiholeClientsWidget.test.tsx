import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useClients } from '@/api/pihole'
import { PiholeClientsWidget, mergeClients } from './PiholeClientsWidget'

vi.mock('@/api/pihole')

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

function err<T = never>(status: number): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<T, ApiError>
}

function pending<T = never>(): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<T, ApiError>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeClientsWidget', () => {
  it('shows loading when total pending', () => {
    vi.mocked(useClients).mockReturnValue(pending())

    render(<PiholeClientsWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows 502 banner when total is 502', () => {
    vi.mocked(useClients).mockReturnValue(err(502))

    render(<PiholeClientsWidget />)
    expect(screen.getByText('Pi-hole clients temporarily unavailable')).toBeInTheDocument()
  })

  it('shows error display when total is non-502 error', () => {
    vi.mocked(useClients).mockReturnValue(err(500))

    render(<PiholeClientsWidget />)
    expect(screen.getByText(/Internal error/i)).toBeInTheDocument()
  })

  it('renders merged clients sorted by total desc', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? ok({
            rows: [{ client: '192.168.2.100', name: 'host', count: 250 }],
            returned: 1,
          })
        : ok({
            rows: [
              { client: '192.168.2.100', name: 'host', count: 1000 },
              { client: '192.168.2.5', name: null, count: 400 },
            ],
            returned: 2,
          }),
    )

    render(<PiholeClientsWidget />)
    const rows = screen.getAllByRole('row')
    // First is header, second should be 192.168.2.100 (1000 queries), third .5 (400)
    if (rows[1] && rows[2]) {
      expect(within(rows[1]).getByText('host')).toBeInTheDocument()
      expect(within(rows[2]).getByText('192.168.2.5')).toBeInTheDocument()
    }
  })

  it('calculates block % correctly', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? ok({
            rows: [{ client: '192.168.2.100', name: 'host', count: 250 }],
            returned: 1,
          })
        : ok({
            rows: [{ client: '192.168.2.100', name: 'host', count: 1000 }],
            returned: 1,
          }),
    )

    render(<PiholeClientsWidget />)
    // 250 / 1000 * 100 = 25.0%
    expect(screen.getByText('25.0%')).toBeInTheDocument()
  })

  it('guards divide-by-zero', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? ok({ rows: [], returned: 0 })
        : ok({
            rows: [{ client: '192.168.2.100', name: null, count: 0 }],
            returned: 1,
          }),
    )

    render(<PiholeClientsWidget />)
    // 0 total → 0.0%, not NaN
    expect(screen.getByText('0.0%')).toBeInTheDocument()
  })

  it('renders with zero blocked when blocked query errors', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? err(500)
        : ok({
            rows: [{ client: '192.168.2.100', name: null, count: 1000 }],
            returned: 1,
          }),
    )

    render(<PiholeClientsWidget />)
    // Should render with 0 blocked, 0.0%
    expect(screen.getByText('1,000')).toBeInTheDocument()
    expect(screen.getByText('0.0%')).toBeInTheDocument()
  })

  it('shows empty state when no total rows', () => {
    vi.mocked(useClients).mockReturnValue(ok({ rows: [], returned: 0 }))

    render(<PiholeClientsWidget />)
    expect(screen.getByTestId('pihole-clients-empty')).toBeInTheDocument()
  })

  it('renders client name when present', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? ok({ rows: [], returned: 0 })
        : ok({
            rows: [{ client: '192.168.2.100', name: 'myhost', count: 100 }],
            returned: 1,
          }),
    )

    render(<PiholeClientsWidget />)
    expect(screen.getByText('myhost')).toBeInTheDocument()
  })

  it('renders only IP when name is null', () => {
    vi.mocked(useClients).mockImplementation((blocked) =>
      blocked
        ? ok({ rows: [], returned: 0 })
        : ok({
            rows: [{ client: '192.168.2.100', name: null, count: 100 }],
            returned: 1,
          }),
    )

    render(<PiholeClientsWidget />)
    expect(screen.getByText('192.168.2.100')).toBeInTheDocument()
  })

  it('unit: mergeClients joins on client IP', () => {
    const total = [
      { client: '192.168.2.100', name: 'host', count: 1000 },
      { client: '192.168.2.5', name: null, count: 400 },
    ]
    const blocked = [{ client: '192.168.2.100', name: 'host', count: 250 }]

    const merged = mergeClients(total, blocked)

    expect(merged).toHaveLength(2)
    expect(merged[0]).toEqual({
      client: '192.168.2.100',
      name: 'host',
      total: 1000,
      blocked: 250,
      blockPct: 25.0,
    })
    expect(merged[1]).toEqual({
      client: '192.168.2.5',
      name: null,
      total: 400,
      blocked: 0,
      blockPct: 0,
    })
  })
})
