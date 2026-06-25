import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { usePiholeOverview } from '@/api/pihole'

import { PiholeLayout } from './PiholeLayout'

vi.mock('@/api/pihole')

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
    Outlet: () => <div data-testid="pihole-outlet" />,
  }
})

type Overview = Schema<'PiholeOverviewResponse'>

const OVERVIEW: Overview = {
  blocking_enabled: true,
  blocking_timer_seconds: null,
  gravity_domains: 1000,
  messages_count: 0,
  percent_blocked: 42,
  privacy_level: 0,
  query_frequency: 7,
  query_logging_enabled: true,
  up: true,
  updates_available: [],
  versions: [],
  query_feed_streaming: false,
}

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

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeLayout', () => {
  it('renders the title, both tab links, and the outlet region', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(OVERVIEW))
    render(<PiholeLayout />)

    expect(screen.getByRole('heading', { name: /pi-hole integration/i })).toBeInTheDocument()
    expect(screen.getByTestId('pihole-tab-overview')).toBeInTheDocument()
    expect(screen.getByTestId('pihole-tab-logs')).toBeInTheDocument()
    expect(screen.getByTestId('pihole-outlet')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Pi-hole tabs' })).toBeInTheDocument()
  })
})
