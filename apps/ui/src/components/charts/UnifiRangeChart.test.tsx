import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useMetricsRange } from '@/api/queries'

import { UnifiRangeChart } from './UnifiRangeChart'

vi.mock('@/api/queries')

// recharts ResponsiveContainer reports 0×0 in jsdom; force a fixed size so the
// inner chart mounts. Keep the rest of recharts real.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => (
      <div style={{ width: 600, height: 140 }}>{children}</div>
    ),
  }
})

type RangeResp = Schema<'MetricsRangeResponse'>

function ok(data: RangeResp): UseQueryResult<RangeResp, ApiError> {
  return {
    data,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: true,
    status: 'success',
  } as UseQueryResult<RangeResp, ApiError>
}

const EMPTY: RangeResp = { status: 'success', data: { resultType: 'matrix', result: [] } }

const WITH_DATA: RangeResp = {
  status: 'success',
  data: {
    resultType: 'matrix',
    result: [
      {
        metric: {},
        values: [
          [1_700_000_000, '100'],
          [1_700_000_600, '200'],
        ],
      },
    ],
  },
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('UnifiRangeChart', () => {
  it('renders the empty state when all series are empty', () => {
    vi.mocked(useMetricsRange).mockReturnValue(ok(EMPTY))
    render(
      <UnifiRangeChart
        title="Speedtest (Mbps)"
        valueFormatter={(v) => `${v}`}
        series={[{ expr: 'a', label: 'A' }]}
      />,
    )
    expect(screen.getByTestId('range-chart-empty')).toBeInTheDocument()
  })

  it('renders the chart figure when data is present', () => {
    vi.mocked(useMetricsRange).mockReturnValue(ok(WITH_DATA))
    render(
      <UnifiRangeChart
        title="Speedtest (Mbps)"
        valueFormatter={(v) => `${v}`}
        series={[
          { expr: 'a', label: 'A' },
          { expr: 'b', label: 'B' },
        ]}
      />,
    )
    expect(screen.getByTestId('range-chart')).toBeInTheDocument()
    expect(screen.getByText('Speedtest (Mbps)')).toBeInTheDocument()
  })

  it('renders the loading placeholder while pending', () => {
    vi.mocked(useMetricsRange).mockReturnValue({
      data: undefined,
      error: null,
      isPending: true,
      isError: false,
      isSuccess: false,
      status: 'pending',
    } as UseQueryResult<RangeResp, ApiError>)
    render(
      <UnifiRangeChart
        title="WAN latency"
        valueFormatter={(v) => `${v}`}
        series={[{ expr: 'a', label: 'A' }]}
      />,
    )
    expect(screen.getByTestId('range-chart-loading')).toBeInTheDocument()
  })

  it('renders the error state on query error', () => {
    const err = new Error('boom') as ApiError
    vi.mocked(useMetricsRange).mockReturnValue({
      data: undefined,
      error: err,
      isPending: false,
      isError: true,
      isSuccess: false,
      status: 'error',
    } as UseQueryResult<RangeResp, ApiError>)
    render(
      <UnifiRangeChart
        title="WAN latency"
        valueFormatter={(v) => `${v}`}
        series={[{ expr: 'a', label: 'A' }]}
      />,
    )
    expect(screen.getByTestId('range-chart-error')).toBeInTheDocument()
  })
})
