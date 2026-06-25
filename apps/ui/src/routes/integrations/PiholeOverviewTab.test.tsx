import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import {
  useAdlists,
  useBlockingMutation,
  useGravityUpdateMutation,
  useMessages,
  usePiholeOverview,
} from '@/api/pihole'

import { PiholeOverviewTab } from './PiholeOverviewTab'

vi.mock('@/api/pihole')
vi.mock('@/routes/integrations/PiholeUpstreamsUnboundWidget')
vi.mock('@/routes/integrations/PiholeClientsWidget')
vi.mock('@/routes/integrations/PiholeRecentBlockedWidget')
vi.mock('@/routes/integrations/PiholeVersionContainerWidget')

type Overview = Schema<'PiholeOverviewResponse'>

const BASE: Overview = {
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
}

function overview(overrides: Partial<Overview> = {}): Overview {
  return { ...BASE, ...overrides }
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

function pending<T>(): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<T, ApiError>
}

function mutationMock<V, R>(
  over: Partial<UseMutationResult<R, ApiError, V>> = {},
): UseMutationResult<R, ApiError, V> {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    isIdle: true,
    error: null,
    data: undefined,
    reset: vi.fn(),
    ...over,
  } as unknown as UseMutationResult<R, ApiError, V>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeOverviewTab', () => {
  beforeEach(() => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ privacy_level: 0 })))
    vi.mocked(useAdlists).mockReturnValue(pending())
    vi.mocked(useMessages).mockReturnValue(pending())
    vi.mocked(useBlockingMutation).mockReturnValue(mutationMock())
    vi.mocked(useGravityUpdateMutation).mockReturnValue(mutationMock())
  })

  it('renders STAGE-006-022 section titles', () => {
    render(<PiholeOverviewTab />)
    expect(screen.getByText('Blocking control')).toBeInTheDocument()
    expect(screen.getByText('Gravity & adlists')).toBeInTheDocument()
    expect(screen.getByText('Messages')).toBeInTheDocument()
  })

  it('renders STAGE-006-023 sections (mocked widgets)', () => {
    render(<PiholeOverviewTab />)
    expect(screen.getByText('Upstreams & Unbound')).toBeInTheDocument()
    expect(screen.getByText('Clients')).toBeInTheDocument()
    expect(screen.getByText('Recent blocked')).toBeInTheDocument()
    expect(screen.getByText('Version & container')).toBeInTheDocument()
  })

  it('does not show 022 "Coming soon" messages', () => {
    render(<PiholeOverviewTab />)
    expect(screen.queryByText(/Coming soon \(STAGE-006-022\)/)).not.toBeInTheDocument()
  })

  it('does not show privacy banner when privacy_level is 0', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ privacy_level: 0 })))
    render(<PiholeOverviewTab />)
    expect(screen.queryByTestId('pihole-privacy-banner')).not.toBeInTheDocument()
  })

  it('does not show privacy banner when privacy_level is null', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ privacy_level: null })))
    render(<PiholeOverviewTab />)
    expect(screen.queryByTestId('pihole-privacy-banner')).not.toBeInTheDocument()
  })

  it('shows privacy banner when privacy_level is 1', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ privacy_level: 1 })))
    render(<PiholeOverviewTab />)
    expect(screen.getByTestId('pihole-privacy-banner')).toBeInTheDocument()
    expect(
      screen.getByText('Query logging restricted — data may be incomplete'),
    ).toBeInTheDocument()
  })

  it('shows privacy banner when privacy_level is greater than 1', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ privacy_level: 5 })))
    render(<PiholeOverviewTab />)
    expect(screen.getByTestId('pihole-privacy-banner')).toBeInTheDocument()
    expect(
      screen.getByText('Query logging restricted — data may be incomplete'),
    ).toBeInTheDocument()
  })
})
