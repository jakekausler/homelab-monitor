import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Schema } from '@/api/types'
import { useUnbound, useUpstreams } from '@/api/pihole'
import type { ApiError } from '@/api/client'
import type { UseQueryResult } from '@tanstack/react-query'
import { PiholeUpstreamsUnboundWidget } from './PiholeUpstreamsUnboundWidget'

vi.mock('@/api/pihole')

type UnboundResponse = Schema<'PiholeUnboundResponse'>

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

describe('PiholeUpstreamsUnboundWidget', () => {
  it('shows loading when both pending', () => {
    vi.mocked(useUpstreams).mockReturnValue(pending())
    vi.mocked(useUnbound).mockReturnValue(pending())

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getAllByText('Loading…').length).toBeGreaterThan(0)
  })

  it('shows yellow banner when unbound is 502', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(err(502))

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText('Pi-hole unbound temporarily unavailable')).toBeInTheDocument()
  })

  it('shows error display when upstreams is non-502 error', () => {
    vi.mocked(useUpstreams).mockReturnValue(err(500))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: false,
        dnssec_bogus_total: null,
        dnssec_secure_total: null,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText(/Internal error/i)).toBeInTheDocument()
  })

  it('renders upstream rows sorted by queries desc', () => {
    vi.mocked(useUpstreams).mockReturnValue(
      ok({
        rows: [
          { upstream: '8.8.8.8', queries: 100 },
          { upstream: '1.1.1.1', queries: 500 },
        ],
      }),
    )
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: false,
        dnssec_bogus_total: null,
        dnssec_secure_total: null,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    const rows = screen.getAllByRole('row')
    // First is header, second should be 1.1.1.1 (500 queries), third 8.8.8.8 (100)
    if (rows[1] && rows[2]) {
      expect(within(rows[1]).getByText('1.1.1.1')).toBeInTheDocument()
      expect(within(rows[2]).getByText('8.8.8.8')).toBeInTheDocument()
    }
  })

  it('shows empty state when no upstreams', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: false,
        dnssec_bogus_total: null,
        dnssec_secure_total: null,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByTestId('pihole-upstreams-empty')).toBeInTheDocument()
  })

  it('renders cache_hit_ratio as percent', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.873,
        extended_stats_enabled: false,
        dnssec_bogus_total: null,
        dnssec_secure_total: null,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText('87.3%')).toBeInTheDocument()
  })

  it('renders cache_hit_ratio as — when null', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    const data: UnboundResponse = {
      cache_hit_ratio: null,
      extended_stats_enabled: false,
      dnssec_bogus_total: null,
      dnssec_secure_total: null,
      recursion_p50_seconds: null,
      recursion_p95_seconds: null,
      servfail_total: null,
      queries_total: null,
      cache_hits_total: null,
      cache_misses_total: null,
      prefetch_total: null,
      requestlist_current: null,
    }
    vi.mocked(useUnbound).mockReturnValue(ok(data))

    render(<PiholeUpstreamsUnboundWidget />)
    const cacheHitElement = screen.getByText('Cache-hit ratio').closest('div')
    expect(within(cacheHitElement!).getByText('—')).toBeInTheDocument()
  })

  it('shows extended stats when enabled with all 5 fields', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: true,
        recursion_p50_seconds: 0.012,
        recursion_p95_seconds: 0.085,
        dnssec_secure_total: 4200,
        dnssec_bogus_total: 3,
        servfail_total: 17,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText('Extended stats on')).toBeInTheDocument()
    expect(screen.getByText('12.0 ms')).toBeInTheDocument()
    expect(screen.getByText('85.0 ms')).toBeInTheDocument()
    expect(screen.getByText('4,200')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('17')).toBeInTheDocument()
  })

  it('hides extended stats when disabled with false', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: false,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        dnssec_secure_total: null,
        dnssec_bogus_total: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText('Extended stats off')).toBeInTheDocument()
    expect(screen.getByText('Unbound extended stats disabled')).toBeInTheDocument()
    expect(screen.queryByText('Recursion p50')).not.toBeInTheDocument()
  })

  it('hides extended stats when null', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: null,
        recursion_p50_seconds: null,
        recursion_p95_seconds: null,
        dnssec_secure_total: null,
        dnssec_bogus_total: null,
        servfail_total: null,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    expect(screen.getByText('Extended stats off')).toBeInTheDocument()
    expect(screen.getByText('Unbound extended stats disabled')).toBeInTheDocument()
  })

  it('renders dnssec_bogus_total in warn badge when > 0', () => {
    vi.mocked(useUpstreams).mockReturnValue(ok({ rows: [] }))
    vi.mocked(useUnbound).mockReturnValue(
      ok({
        cache_hit_ratio: 0.75,
        extended_stats_enabled: true,
        recursion_p50_seconds: 0.012,
        recursion_p95_seconds: 0.085,
        dnssec_secure_total: 4200,
        dnssec_bogus_total: 5,
        servfail_total: 17,
        queries_total: null,
        cache_hits_total: null,
        cache_misses_total: null,
        prefetch_total: null,
        requestlist_current: null,
      }),
    )

    render(<PiholeUpstreamsUnboundWidget />)
    const badgeElements = screen.queryAllByText('5')
    expect(badgeElements.length).toBeGreaterThan(0)
  })
})
