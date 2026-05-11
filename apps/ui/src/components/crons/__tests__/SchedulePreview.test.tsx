import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SchedulePreviewForExpr, SchedulePreviewForSaved } from '@/components/crons/SchedulePreview'

afterEach(cleanup)

// Project test conventions:
// - Framework: Vitest with vi.mock()
// - Providers: QueryClientProvider wrapping
// - Async: await screen.findByText for async content

vi.mock('@/api/crons', () => ({
  usePreviewExpr: vi.fn(),
  usePreviewSavedCron: vi.fn(),
}))

vi.mock('@/lib/relativeTime', () => ({
  formatAbsolute: (s: string) => `formatted:${s}`,
}))

import { usePreviewExpr, usePreviewSavedCron } from '@/api/crons'

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient()
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('SchedulePreviewForExpr', () => {
  it('shows placeholder when expr is empty', () => {
    wrap(<SchedulePreviewForExpr expr="" />)
    expect(screen.getByText(/Enter a schedule to preview/i)).toBeInTheDocument()
  })

  it('shows loading state while calculating', () => {
    vi.mocked(usePreviewExpr).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof usePreviewExpr>)
    wrap(<SchedulePreviewForExpr expr="* * * * *" />)
    expect(screen.getByText(/Calculating/i)).toBeInTheDocument()
  })

  it('shows error message when query fails', () => {
    vi.mocked(usePreviewExpr).mockReturnValue({
      isLoading: false,
      error: { message: 'bad expr' } as Error,
      data: undefined,
    } as unknown as ReturnType<typeof usePreviewExpr>)
    wrap(<SchedulePreviewForExpr expr="bad" />)
    expect(screen.getByRole('alert')).toHaveTextContent('bad expr')
  })

  it('renders formatted run timestamps', () => {
    vi.mocked(usePreviewExpr).mockReturnValue({
      isLoading: false,
      error: null,
      data: { runs: ['2026-05-11T04:00:00Z', '2026-05-12T04:00:00Z'] },
    } as unknown as ReturnType<typeof usePreviewExpr>)
    wrap(<SchedulePreviewForExpr expr="0 4 * * *" />)
    expect(screen.getByText('formatted:2026-05-11T04:00:00Z')).toBeInTheDocument()
    expect(screen.getByText('formatted:2026-05-12T04:00:00Z')).toBeInTheDocument()
  })

  it('shows no upcoming runs when runs list is empty', () => {
    vi.mocked(usePreviewExpr).mockReturnValue({
      isLoading: false,
      error: null,
      data: { runs: [] },
    } as unknown as ReturnType<typeof usePreviewExpr>)
    wrap(<SchedulePreviewForExpr expr="0 4 * * *" />)
    expect(screen.getByText(/No upcoming runs/i)).toBeInTheDocument()
  })
})

describe('SchedulePreviewForSaved', () => {
  it('shows loading state', () => {
    vi.mocked(usePreviewSavedCron).mockReturnValue({
      isLoading: true,
      error: null,
      data: undefined,
    } as unknown as ReturnType<typeof usePreviewSavedCron>)
    wrap(<SchedulePreviewForSaved cronId="c1" />)
    expect(screen.getByText(/Calculating/i)).toBeInTheDocument()
  })

  it('shows unavailable message on error', () => {
    vi.mocked(usePreviewSavedCron).mockReturnValue({
      isLoading: false,
      error: { message: 'timeout' } as Error,
      data: undefined,
    } as unknown as ReturnType<typeof usePreviewSavedCron>)
    wrap(<SchedulePreviewForSaved cronId="c1" />)
    expect(screen.getByText(/Schedule preview unavailable/i)).toBeInTheDocument()
  })

  it('renders run list on success', () => {
    vi.mocked(usePreviewSavedCron).mockReturnValue({
      isLoading: false,
      error: null,
      data: { runs: ['2026-05-11T04:00:00Z'] },
    } as unknown as ReturnType<typeof usePreviewSavedCron>)
    wrap(<SchedulePreviewForSaved cronId="c1" />)
    expect(screen.getByText('formatted:2026-05-11T04:00:00Z')).toBeInTheDocument()
  })
})
