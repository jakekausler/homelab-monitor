import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { useMessages } from '@/api/pihole'

import { PiholeMessagesWidget } from './PiholeMessagesWidget'

vi.mock('@/api/pihole')

type Messages = Schema<'PiholeMessagesResponse'>

const BASE: Messages = {
  returned: 0,
  total: 0,
  rows: [],
}

function messages(overrides: Partial<Messages> = {}): Messages {
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

function err(status: number): UseQueryResult<Messages, ApiError> {
  return {
    data: undefined,
    error: { status } as ApiError,
    isPending: false,
    isError: true,
    isSuccess: false,
    status: 'error',
  } as UseQueryResult<Messages, ApiError>
}

function pending(): UseQueryResult<Messages, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: true,
    isError: false,
    isSuccess: false,
    status: 'pending',
  } as UseQueryResult<Messages, ApiError>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeMessagesWidget', () => {
  it('shows Loading… while pending', () => {
    vi.mocked(useMessages).mockReturnValue(pending())
    render(<PiholeMessagesWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the yellow temporarily-unavailable banner on 502', () => {
    vi.mocked(useMessages).mockReturnValue(err(502))
    render(<PiholeMessagesWidget />)
    expect(screen.getByText('Pi-hole messages temporarily unavailable')).toBeInTheDocument()
  })

  it('renders ErrorDisplay on a non-502 error', () => {
    vi.mocked(useMessages).mockReturnValue(err(500))
    render(<PiholeMessagesWidget />)
    expect(screen.getByText(/Internal error/)).toBeInTheDocument()
  })

  it('shows EmptyState when no messages', () => {
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows: [] })))
    render(<PiholeMessagesWidget />)
    expect(screen.getByTestId('pihole-messages-empty')).toBeInTheDocument()
    expect(screen.getByText('No diagnostic messages')).toBeInTheDocument()
  })

  it('renders message list with type badge, message text, and timestamp', () => {
    const rows = [
      {
        id: 1,
        type: 'warning',
        message: 'Test message 1',
        timestamp: 1700000000,
        url: null,
      },
      {
        id: 2,
        type: 'error',
        message: 'Test message 2',
        timestamp: null,
        url: 'https://example.com',
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows, returned: 2, total: 2 })))
    render(<PiholeMessagesWidget />)

    expect(screen.getByText('Test message 1')).toBeInTheDocument()
    expect(screen.getByText('Test message 2')).toBeInTheDocument()
  })

  it('formats timestamp correctly from epoch seconds', () => {
    const epochSeconds = 1700000000 // 2023-11-14 22:13:20 UTC
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message with timestamp',
        timestamp: epochSeconds,
        url: null,
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    const expected =
      new Date(epochSeconds * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
    expect(screen.getByText(expected)).toBeInTheDocument()
  })

  it('does not render timestamp when null', () => {
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message without timestamp',
        timestamp: null,
        url: null,
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    expect(screen.getByText('Message without timestamp')).toBeInTheDocument()
    // Ensure no UTC text appears
    const utcElements = screen.queryAllByText(/UTC/)
    expect(utcElements.length).toBe(0)
  })

  it('renders url anchor when url is present', () => {
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message with link',
        timestamp: null,
        url: 'https://example.com/help',
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://example.com/help')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('does not render url anchor when url is null', () => {
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message without link',
        timestamp: null,
        url: null,
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('does not render url anchor when url is empty string', () => {
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message with empty url',
        timestamp: null,
        url: '',
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('does not render url anchor for non-http(s) urls (e.g. javascript:)', () => {
    const rows = [
      {
        id: 1,
        type: 'info',
        message: 'Message with javascript url',
        timestamp: null,
        url: 'javascript:alert("xss")',
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('renders type badge for each message', () => {
    const rows = [
      {
        id: 1,
        type: 'warning',
        message: 'Warning message',
        timestamp: null,
        url: null,
      },
      {
        id: 2,
        type: 'error',
        message: 'Error message',
        timestamp: null,
        url: null,
      },
    ] as Schema<'PiholeMessageRow'>[]
    vi.mocked(useMessages).mockReturnValue(ok(messages({ rows })))
    render(<PiholeMessagesWidget />)

    expect(screen.getByText('warning')).toBeInTheDocument()
    expect(screen.getByText('error')).toBeInTheDocument()
  })
})
