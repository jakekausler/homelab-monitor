import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useSurveillanceSummary } from '@/api/surveillance'

import { SurveillanceLayout } from './SurveillanceLayout'

type SurveillanceSummary = Schema<'SurveillanceSummary'>

vi.mock('@/api/surveillance')

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
    Outlet: () => <div data-testid="surveillance-outlet" />,
  }
})

function makeResult(overrides: Partial<UseQueryResult<SurveillanceSummary, ApiError>>) {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    status: 'pending',
    ...overrides,
  } as UseQueryResult<SurveillanceSummary, ApiError>
}

const MOCK_SUMMARY: SurveillanceSummary = {
  license_used: 3,
  license_max: 90,
  homemode_on: false,
  cameras_total: 3,
  cameras_connected_total: 3,
  cameras_disconnected_total: 0,
  data_available: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useSurveillanceSummary).mockReturnValue(
    makeResult({ data: MOCK_SUMMARY, isSuccess: true, status: 'success' }),
  )
})

describe('SurveillanceLayout', () => {
  it('renders header, the Cameras and Activity tabs, and outlet', () => {
    render(<SurveillanceLayout />)
    expect(screen.getByRole('heading', { name: /surveillance/i })).toBeInTheDocument()
    expect(screen.getByTestId('surveillance-tab-cameras')).toHaveTextContent('Cameras')
    expect(screen.getByTestId('surveillance-tab-activity')).toHaveTextContent('Activity')
    expect(screen.getByTestId('surveillance-outlet')).toBeInTheDocument()
  })

  it('renders the status strip chips from summary', () => {
    render(<SurveillanceLayout />)
    const strip = screen.getByTestId('surveillance-status-strip')
    expect(strip).toHaveTextContent(/License 3\/90/i)
    expect(strip).toHaveTextContent(/HomeMode: Off/i)
    expect(strip).toHaveTextContent(/Cameras 3\/3/i)
  })

  it('renders HomeMode On (warn) and Cameras connected<total (warn)', () => {
    vi.mocked(useSurveillanceSummary).mockReturnValue(
      makeResult({
        data: { ...MOCK_SUMMARY, homemode_on: true, cameras_connected_total: 2 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SurveillanceLayout />)
    const strip = screen.getByTestId('surveillance-status-strip')
    expect(strip).toHaveTextContent(/HomeMode: On/i)
    expect(strip).toHaveTextContent(/Cameras 2\/3/i)
  })

  it('renders the License — / Cameras — fallbacks for null scalars', () => {
    vi.mocked(useSurveillanceSummary).mockReturnValue(
      makeResult({
        data: {
          ...MOCK_SUMMARY,
          license_used: null,
          license_max: null,
          cameras_total: null,
          cameras_connected_total: null,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SurveillanceLayout />)
    const strip = screen.getByTestId('surveillance-status-strip')
    expect(strip).toHaveTextContent(/License —/i)
    expect(strip).toHaveTextContent(/Cameras —/i)
  })

  it('renders the collector-not-run state when data_available is false', () => {
    vi.mocked(useSurveillanceSummary).mockReturnValue(
      makeResult({
        data: { ...MOCK_SUMMARY, data_available: false },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SurveillanceLayout />)
    expect(screen.getByText(/Surveillance collector has not run yet/i)).toBeInTheDocument()
  })

  it('shows the 502 unavailable banner', () => {
    const err = new Error('bad gateway') as ApiError
    ;(err as { status: number }).status = 502
    vi.mocked(useSurveillanceSummary).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<SurveillanceLayout />)
    expect(screen.getByText(/Surveillance metrics temporarily unavailable/i)).toBeInTheDocument()
  })
})
