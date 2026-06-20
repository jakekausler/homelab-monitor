import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useUnifiSummary } from '@/api/unifi'

import { UnifiLayout } from './UnifiLayout'

type UnifiSummary = Schema<'UnifiSummary'>

vi.mock('@/api/unifi')

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
    Outlet: () => <div data-testid="unifi-outlet" />,
  }
})

function makeResult(overrides: Partial<UseQueryResult<UnifiSummary, ApiError>>) {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    status: 'pending',
    ...overrides,
  } as UseQueryResult<UnifiSummary, ApiError>
}

const MOCK_SUMMARY: UnifiSummary = {
  controller_up: true,
  controller_reason: null,
  wan_up: true,
  teleport_up: true,
  devices_total: 5,
  devices_up: 5,
  threat_count: 0,
  last_seen: '2026-06-12T00:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useUnifiSummary).mockReturnValue(
    makeResult({ data: MOCK_SUMMARY, isSuccess: true, status: 'success' }),
  )
})

describe('UnifiLayout', () => {
  it('renders header, tab, and outlet', () => {
    render(<UnifiLayout />)
    expect(screen.getByRole('heading', { name: /unifi integration/i })).toBeInTheDocument()
    expect(screen.getByTestId('unifi-tab-overview')).toHaveTextContent('Overview')
    expect(screen.getByTestId('unifi-outlet')).toBeInTheDocument()
  })

  it('renders the persistent status strip from summary', () => {
    render(<UnifiLayout />)
    const strip = screen.getByTestId('unifi-status-strip')
    expect(strip).toHaveTextContent(/Controller up/i)
    expect(strip).toHaveTextContent(/WAN up/i)
    expect(strip).toHaveTextContent('5/5')
  })

  it('shows the 502 unavailable banner', () => {
    const err = new Error('bad gateway') as ApiError
    ;(err as { status: number }).status = 502
    vi.mocked(useUnifiSummary).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<UnifiLayout />)
    expect(screen.getByText(/Unifi metrics temporarily unavailable/i)).toBeInTheDocument()
  })
})
