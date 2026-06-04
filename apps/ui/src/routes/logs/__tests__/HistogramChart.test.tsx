import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

import { HistogramChart } from '../HistogramChart'
import { useLogsHistogramQuery } from '@/api/logs'
import type { Schema } from '@/api/types'

// Mock the data hook.
vi.mock('@/api/logs', () => ({
  useLogsHistogramQuery: vi.fn(),
}))

// Partial-mock recharts: ResponsiveContainer -> fixed-size div (jsdom has no
// layout); BarChart -> renders one clickable cell per data row so the onClick
// path is deterministic; Bar/axes/grid/tooltip -> render their dataKey marker.
vi.mock('recharts', () => {
  interface Row {
    label: string
    startMs: number
    total: number
  }
  return {
    ResponsiveContainer: ({ children }: { children: ReactNode }) => (
      <div style={{ width: 400, height: 96 }}>{children}</div>
    ),
    BarChart: ({
      data,
      onClick,
      children,
    }: {
      data: Row[]
      onClick?: (s: { activeTooltipIndex?: number }) => void
      children: ReactNode
    }) => (
      <div data-testid="mock-barchart">
        {data.map((row, i) => (
          <button
            key={row.startMs}
            type="button"
            data-testid="histogram-bar"
            data-index={i}
            onClick={() => onClick?.({ activeTooltipIndex: i })}
          >
            {row.label}
          </button>
        ))}
        {children}
      </div>
    ),
    Bar: ({ dataKey }: { dataKey: string }) => <div data-testid={`bar-${dataKey}`} />,
    XAxis: () => <div data-testid="x-axis" />,
    YAxis: () => <div data-testid="y-axis" />,
    Tooltip: () => <div data-testid="tooltip" />,
    CartesianGrid: () => <div data-testid="grid" />,
  }
})

afterEach(() => {
  cleanup()
})

type LogsHistogramResponse = Schema<'LogsHistogramResponse'>

const SAMPLE: LogsHistogramResponse = {
  bucket_duration_ms: 60_000,
  buckets: [
    {
      start_ts: '2026-05-07T00:00:00.000Z',
      counts_by_severity: { error: 2, warn: 1, info: 5 },
      total: 8,
    },
    {
      start_ts: '2026-05-07T00:01:00.000Z',
      counts_by_severity: { error: 0, warn: 0, info: 3 },
      total: 3,
    },
  ],
}

function mockQuery(overrides: Partial<ReturnType<typeof useLogsHistogramQuery>>): void {
  vi.mocked(useLogsHistogramQuery).mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    ...overrides,
  } as ReturnType<typeof useLogsHistogramQuery>)
}

function renderChart(onNarrowRange = vi.fn()) {
  return {
    onNarrowRange,
    ...render(
      <HistogramChart
        expr="*"
        start="2026-05-07T00:00:00Z"
        end="2026-05-07T01:00:00Z"
        services=""
        onNarrowRange={onNarrowRange}
      />,
    ),
  }
}

describe('HistogramChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders one bar per bucket and all three stacked severity series', () => {
    mockQuery({ data: SAMPLE })
    renderChart()
    expect(screen.getAllByTestId('histogram-bar')).toHaveLength(2)
    expect(screen.getByTestId('bar-error')).toBeInTheDocument()
    expect(screen.getByTestId('bar-warn')).toBeInTheDocument()
    expect(screen.getByTestId('bar-info')).toBeInTheDocument()
  })

  it('clicking a bar narrows the range to [start, start+duration)', () => {
    const onNarrow = vi.fn()
    mockQuery({ data: SAMPLE })
    renderChart(onNarrow)
    const bars = screen.getAllByTestId('histogram-bar')
    fireEvent.click(bars[0]!)
    expect(onNarrow).toHaveBeenCalledWith(
      '2026-05-07T00:00:00.000Z',
      '2026-05-07T00:01:00.000Z', // start + 60_000ms
    )
  })

  it('clicking the second bar uses that bucket window', () => {
    const onNarrow = vi.fn()
    mockQuery({ data: SAMPLE })
    renderChart(onNarrow)
    const bars = screen.getAllByTestId('histogram-bar')
    fireEvent.click(bars[1]!)
    expect(onNarrow).toHaveBeenCalledWith('2026-05-07T00:01:00.000Z', '2026-05-07T00:02:00.000Z')
  })

  it('shows loading state', () => {
    mockQuery({ isLoading: true })
    renderChart()
    expect(screen.getByTestId('histogram-loading')).toBeInTheDocument()
  })

  it('shows error state (chart suppressed)', () => {
    mockQuery({ isError: true })
    renderChart()
    expect(screen.getByTestId('histogram-error')).toBeInTheDocument()
    expect(screen.queryByTestId('histogram-chart')).not.toBeInTheDocument()
  })

  it('shows empty state when all buckets are zero', () => {
    mockQuery({
      data: {
        bucket_duration_ms: 60_000,
        buckets: [
          {
            start_ts: '2026-05-07T00:00:00.000Z',
            counts_by_severity: { error: 0, warn: 0, info: 0 },
            total: 0,
          },
        ],
      },
    })
    renderChart()
    expect(screen.getByTestId('histogram-empty')).toBeInTheDocument()
  })
})
