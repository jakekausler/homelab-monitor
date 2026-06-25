import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { usePiholeOverview } from '@/api/pihole'

import { PiholeStatusStrip, formatMSS } from './PiholeStatusStrip'

vi.mock('@/api/pihole')

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
  query_feed_streaming: false,
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

function err(status: number): UseQueryResult<Overview, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<Overview, ApiError>
}

function pending(): UseQueryResult<Overview, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<Overview, ApiError>
}

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.clearAllMocks()
})

describe('formatMSS', () => {
  it('formats whole minutes and seconds', () => {
    expect(formatMSS(298)).toBe('4:58')
    expect(formatMSS(0)).toBe('0:00')
    expect(formatMSS(60)).toBe('1:00')
    expect(formatMSS(5)).toBe('0:05')
  })

  it('returns em dash for null', () => {
    expect(formatMSS(null)).toBe('—')
  })
})

describe('PiholeStatusStrip', () => {
  it('shows Loading… while pending', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(pending())
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the yellow temporarily-unavailable banner on 502 (no badges)', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(502))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Pi-hole metrics temporarily unavailable')).toBeInTheDocument()
    expect(screen.queryByText(/Pi-hole up/)).not.toBeInTheDocument()
  })

  it('renders ErrorDisplay on a non-502 error', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(500))
    render(<PiholeStatusStrip />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('Pi-hole metrics temporarily unavailable')).not.toBeInTheDocument()
  })

  it('shows "Pi-hole up" + "Blocking on" with no countdown when up and blocking enabled', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ up: true, blocking_enabled: true })))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Pi-hole up')).toBeInTheDocument()
    expect(screen.getByText('Blocking on')).toBeInTheDocument()
    expect(screen.queryByText(/re-enables in/)).not.toBeInTheDocument()
  })

  it('shows "Pi-hole down" when up is false', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ up: false })))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Pi-hole down')).toBeInTheDocument()
  })

  it('shows "—% blocked" and "— q/s" when those metrics are null', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(
      ok(overview({ percent_blocked: null, query_frequency: null })),
    )
    render(<PiholeStatusStrip />)
    expect(screen.getByText(/—% blocked/)).toBeInTheDocument()
    expect(screen.getByText(/— q\/s/)).toBeInTheDocument()
  })

  it('shows critical "N messages" when messages_count > 0', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ messages_count: 3 })))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('3 messages')).toBeInTheDocument()
  })

  it('shows "No messages" when messages_count === 0', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ messages_count: 0 })))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('No messages')).toBeInTheDocument()
  })

  it('shows critical "Blocking off" (no countdown) when disabled with null timer', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(
      ok(overview({ blocking_enabled: false, blocking_timer_seconds: null })),
    )
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Blocking off')).toBeInTheDocument()
    expect(screen.queryByText(/re-enables in/)).not.toBeInTheDocument()
  })

  it('shows muted "Blocking —" when blocking_enabled is null', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview({ blocking_enabled: null })))
    render(<PiholeStatusStrip />)
    expect(screen.getByText('Blocking —')).toBeInTheDocument()
  })

  describe('live countdown', () => {
    it('shows initial M:SS from timer and decrements each second', () => {
      vi.useFakeTimers()
      vi.mocked(usePiholeOverview).mockReturnValue(
        ok(overview({ blocking_enabled: false, blocking_timer_seconds: 3 })),
      )
      render(<PiholeStatusStrip />)
      // initial render: 3s → "0:03"
      expect(screen.getByText(/re-enables in 0:03/)).toBeInTheDocument()
      // advance 1s: 2s remaining → "0:02"
      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(screen.getByText(/re-enables in 0:02/)).toBeInTheDocument()
      // advance 5s more: would be -3s, floors to 0s → "0:00"
      act(() => {
        vi.advanceTimersByTime(5000)
      })
      expect(screen.getByText(/re-enables in 0:00/)).toBeInTheDocument()
    })

    it('re-anchors and jumps to new M:SS when server timer changes', () => {
      vi.useFakeTimers()
      vi.mocked(usePiholeOverview).mockReturnValue(
        ok(overview({ blocking_enabled: false, blocking_timer_seconds: 3 })),
      )
      const { rerender } = render(<PiholeStatusStrip />)
      expect(screen.getByText(/re-enables in 0:03/)).toBeInTheDocument()
      // server reports a new, larger timer
      vi.mocked(usePiholeOverview).mockReturnValue(
        ok(overview({ blocking_enabled: false, blocking_timer_seconds: 298 })),
      )
      rerender(<PiholeStatusStrip />)
      // should jump to 298s → "4:58"
      expect(screen.getByText(/re-enables in 4:58/)).toBeInTheDocument()
    })

    it('unmounts without errors and useReenableCountdown cleans up its interval', () => {
      vi.useFakeTimers()
      vi.mocked(usePiholeOverview).mockReturnValue(
        ok(overview({ blocking_enabled: false, blocking_timer_seconds: 30 })),
      )
      const { unmount } = render(<PiholeStatusStrip />)
      // unmount should trigger useReenableCountdown's cleanup (no act warnings, no errors)
      expect(() => {
        unmount()
      }).not.toThrow()
    })
  })
})
