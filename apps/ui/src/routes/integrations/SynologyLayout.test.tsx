import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useSynologySummary } from '@/api/synology'

import { SynologyLayout } from './SynologyLayout'

type SynologySummary = Schema<'SynologySummary'>

vi.mock('@/api/synology')

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({
      children,
      to,
      ...rest
    }: {
      children: ReactNode
      to: string
      'data-testid'?: string
    }) => (
      <a href={to} data-testid={rest['data-testid']}>
        {children}
      </a>
    ),
    Outlet: () => <div data-testid="synology-outlet" />,
  }
})

function makeResult(overrides: Partial<UseQueryResult<SynologySummary, ApiError>>) {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    status: 'pending',
    ...overrides,
  } as UseQueryResult<SynologySummary, ApiError>
}

const MOCK_SUMMARY: SynologySummary = {
  dsm_up: true,
  volume_used_percent_max: 42,
  ups_on_battery: false,
  ups_charge_percent: 100,
  update_available: false,
  security_safe: true,
  backup_configured: true,
  last_seen: '2026-06-12T00:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useSynologySummary).mockReturnValue(
    makeResult({ data: MOCK_SUMMARY, isSuccess: true, status: 'success' }),
  )
})

describe('SynologyLayout', () => {
  it('renders header, tabs, and outlet', () => {
    render(<SynologyLayout />)
    expect(screen.getByRole('heading', { name: /synology integration/i })).toBeInTheDocument()
    expect(screen.getByTestId('synology-tab-hardware')).toHaveTextContent('Hardware')
    expect(screen.getByTestId('synology-tab-ops')).toHaveTextContent('Ops')
    expect(screen.queryByTestId('synology-tab-overview')).not.toBeInTheDocument()
    expect(screen.queryByTestId('synology-tab-metrics')).not.toBeInTheDocument()
    expect(screen.getByTestId('synology-outlet')).toBeInTheDocument()
  })

  it('renders the status strip chips from summary', () => {
    render(<SynologyLayout />)
    const strip = screen.getByTestId('synology-status-strip')
    expect(strip).toHaveTextContent(/System health: OK/i)
    expect(strip).toHaveTextContent(/Volume: 42%/i)
    expect(strip).toHaveTextContent(/UPS: online \(100%\)/i)
    expect(strip).toHaveTextContent(/Updated/i)
  })

  it('shows the 502 unavailable banner', () => {
    const err = new Error('bad gateway') as ApiError
    ;(err as { status: number }).status = 502
    vi.mocked(useSynologySummary).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<SynologyLayout />)
    expect(screen.getByText(/Synology metrics temporarily unavailable/i)).toBeInTheDocument()
  })

  it('shows Degraded (never "down") when dsm_up is false and high volume as critical', () => {
    vi.mocked(useSynologySummary).mockReturnValue(
      makeResult({
        data: { ...MOCK_SUMMARY, dsm_up: false, volume_used_percent_max: 95 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyLayout />)
    const strip = screen.getByTestId('synology-status-strip')
    expect(strip).toHaveTextContent(/System health: Degraded/i)
    expect(strip).not.toHaveTextContent(/down/i)
    expect(strip).toHaveTextContent(/Volume: 95%/i)
  })

  it('renders UPS on-battery state with charge', () => {
    vi.mocked(useSynologySummary).mockReturnValue(
      makeResult({
        data: { ...MOCK_SUMMARY, ups_on_battery: true, ups_charge_percent: 55 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SynologyLayout />)
    expect(screen.getByTestId('synology-status-strip')).toHaveTextContent(
      /UPS: on battery \(55%\)/i,
    )
  })
})
