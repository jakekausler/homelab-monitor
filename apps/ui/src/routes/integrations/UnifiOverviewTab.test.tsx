import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import {
  useUnifiControllerHealth,
  useUnifiDevices,
  useUnifiDpi,
  useUnifiTeleport,
  useUnifiThreats,
} from '@/api/unifi'

import { UnifiOverviewTab } from './UnifiOverviewTab'

vi.mock('@/api/unifi')

// Stub the TanStack Link so the device-table links render as plain anchors.
vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({ children, ...rest }: { children: ReactNode; 'data-testid'?: string }) => (
      <a data-testid={rest['data-testid']}>{children}</a>
    ),
  }
})

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

const DEVICES: Schema<'UnifiDevicesResponse'> = {
  devices: [
    {
      mac: 'aa:bb:cc:dd:ee:ff',
      name: 'Living Room AP',
      model: 'U6-Pro',
      kind: 'ap',
      up: true,
      cpu_pct: 12,
      mem_pct: 40,
      temp: 45,
      uptime_seconds: 3600,
      update_available: true,
    },
  ],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  vi.mocked(useUnifiDevices).mockReturnValue(ok(DEVICES))
  vi.mocked(useUnifiThreats).mockReturnValue(ok({ threats: [] }))
  vi.mocked(useUnifiDpi).mockReturnValue(ok({ apps: [] }))
  vi.mocked(useUnifiTeleport).mockReturnValue(
    ok({ teleport_up: true, version: '1.2.3', reason: null }),
  )
  vi.mocked(useUnifiControllerHealth).mockReturnValue(
    ok({ controller_up: true, up_reasons: [], api_took_seconds: [] }),
  )
})

describe('UnifiOverviewTab', () => {
  it('renders device rows with name and update badge', () => {
    render(<UnifiOverviewTab />)
    expect(screen.getByText('Living Room AP')).toBeInTheDocument()
    expect(screen.getByText('update')).toBeInTheDocument()
    expect(screen.getByText('U6-Pro')).toBeInTheDocument()
  })

  it('renders honest empty states for empty threats and dpi', () => {
    render(<UnifiOverviewTab />)
    expect(screen.getByText('No active threats')).toBeInTheDocument()
    expect(screen.getByText('No DPI data')).toBeInTheDocument()
  })

  it('renders DPI rows when present', () => {
    vi.mocked(useUnifiDpi).mockReturnValue(
      ok({ apps: [{ app: 'YouTube', bytes: 1536, cat: 'Streaming', client: 'tv' }] }),
    )
    render(<UnifiOverviewTab />)
    expect(screen.getByText('YouTube')).toBeInTheDocument()
    expect(screen.getByText('1.5 KiB')).toBeInTheDocument()
  })

  it('shows loading state for a pending widget', () => {
    vi.mocked(useUnifiThreats).mockReturnValue({
      data: undefined,
      error: null,
      isPending: true,
      isError: false,
      isSuccess: false,
      status: 'pending',
    } as UseQueryResult<Schema<'UnifiThreatsResponse'>, ApiError>)
    render(<UnifiOverviewTab />)
    expect(screen.getAllByText('Loading…').length).toBeGreaterThan(0)
  })

  it('renders threat rows when threats list is non-empty', () => {
    vi.mocked(useUnifiThreats).mockReturnValue(
      ok({
        threats: [
          {
            threat_type: 'ET MALWARE',
            count: 1,
          },
        ],
      }),
    )
    render(<UnifiOverviewTab />)
    expect(screen.getByText('ET MALWARE')).toBeInTheDocument()
  })

  it('renders controller health endpoint rows when up_reasons and api_took_seconds are non-empty', () => {
    vi.mocked(useUnifiControllerHealth).mockReturnValue(
      ok({
        controller_up: true,
        up_reasons: ['network reachable'],
        api_took_seconds: [{ endpoint: '/api/v2/devices', seconds: 0.042 }],
      }),
    )
    render(<UnifiOverviewTab />)
    expect(screen.getByText('network reachable')).toBeInTheDocument()
    expect(screen.getByText('/api/v2/devices')).toBeInTheDocument()
  })

  it('renders teleport reason when set', () => {
    vi.mocked(useUnifiTeleport).mockReturnValue(
      ok({ teleport_up: false, version: null, reason: 'no license' }),
    )
    render(<UnifiOverviewTab />)
    expect(screen.getByText('no license')).toBeInTheDocument()
  })
})
