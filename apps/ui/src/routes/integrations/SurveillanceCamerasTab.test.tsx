import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useSurveillanceCameras } from '@/api/surveillance'

import { cameraConnectedBadge } from './surveillanceFormat'
import { SurveillanceCamerasTab } from './SurveillanceCamerasTab'

type SurveillanceCameras = Schema<'SurveillanceCameras'>

vi.mock('@/api/surveillance')

function makeResult<T>(
  overrides: Partial<UseQueryResult<T, ApiError>>,
): UseQueryResult<T, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    status: 'pending',
    ...overrides,
  } as UseQueryResult<T, ApiError>
}

const MOCK_CAMERAS: SurveillanceCameras = {
  cameras: [
    {
      camera: 'FrontDoor',
      connected: true,
      status: 3,
      recordings_count: 42,
      model: 'TV-IP1314PI',
      ip: '192.168.2.50',
      vendor: 'TRENDnet',
    },
    {
      camera: 'Garage',
      connected: false,
      status: null,
      recordings_count: null,
      model: null,
      ip: null,
      vendor: null,
    },
    {
      camera: 'Backyard',
      connected: true,
      status: 1,
      recordings_count: 0,
      model: 'AXIS-M1065',
      ip: '192.168.2.51',
      vendor: 'Axis',
    },
  ],
  events_today: 5,
  events_total_all: 1000,
  recordings_total: 777,
  data_available: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useSurveillanceCameras).mockReturnValue(
    makeResult<SurveillanceCameras>({
      data: MOCK_CAMERAS,
      isSuccess: true,
      status: 'success',
    }),
  )
})

describe('SurveillanceCamerasTab', () => {
  it('renders one card per camera with name, connected badge, model/ip/vendor', () => {
    render(<SurveillanceCamerasTab />)
    const front = screen.getByTestId('surveillance-camera-card-FrontDoor')
    expect(within(front).getByText('FrontDoor')).toBeInTheDocument()
    expect(within(front).getByText('Connected')).toBeInTheDocument()
    expect(within(front).getByText('TV-IP1314PI')).toBeInTheDocument()
    expect(within(front).getByText('192.168.2.50')).toBeInTheDocument()
    expect(within(front).getByText('TRENDnet')).toBeInTheDocument()
    expect(within(front).getByText('42')).toBeInTheDocument()
  })

  it('renders the disconnected badge and em-dashes for null fields', () => {
    render(<SurveillanceCamerasTab />)
    const garage = screen.getByTestId('surveillance-camera-card-Garage')
    expect(within(garage).getByText('Disconnected')).toBeInTheDocument()
    // model, ip, vendor, status, recordings all null -> five em-dashes
    expect(within(garage).getAllByText('—').length).toBe(5)
  })

  it('renders recordings_count 0 as "0" (not em-dash)', () => {
    render(<SurveillanceCamerasTab />)
    const backyard = screen.getByTestId('surveillance-camera-card-Backyard')
    // recordings_count 0 and status 1 both render as digits, not "—"
    expect(within(backyard).getByText('0')).toBeInTheDocument()
    expect(within(backyard).queryByText('—')).not.toBeInTheDocument()
  })

  it('renders the 0-cameras empty state when data_available and cameras empty', () => {
    vi.mocked(useSurveillanceCameras).mockReturnValue(
      makeResult<SurveillanceCameras>({
        data: { ...MOCK_CAMERAS, cameras: [] },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SurveillanceCamerasTab />)
    expect(screen.getByTestId('surveillance-cameras-empty')).toBeInTheDocument()
  })

  it('renders the unavailable empty state when data_available is false', () => {
    vi.mocked(useSurveillanceCameras).mockReturnValue(
      makeResult<SurveillanceCameras>({
        data: { ...MOCK_CAMERAS, cameras: [], data_available: false },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<SurveillanceCamerasTab />)
    expect(screen.getByTestId('surveillance-cameras-unavailable')).toBeInTheDocument()
  })

  it('renders the 502 unavailable banner', () => {
    vi.mocked(useSurveillanceCameras).mockReturnValue(
      makeResult<SurveillanceCameras>({
        error: { status: 502 } as ApiError,
        isError: true,
        status: 'error',
      }),
    )
    render(<SurveillanceCamerasTab />)
    expect(
      screen.getByText('Surveillance camera metrics temporarily unavailable'),
    ).toBeInTheDocument()
  })

  it('maps connected to badge tone/label', () => {
    expect(cameraConnectedBadge(true)).toEqual({ variant: 'ok', label: 'Connected' })
    expect(cameraConnectedBadge(false)).toEqual({ variant: 'critical', label: 'Disconnected' })
  })
})
