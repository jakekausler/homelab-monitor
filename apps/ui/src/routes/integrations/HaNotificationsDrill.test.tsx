import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { useHomeAssistantNotifications } from '@/api/home_assistant'

import { HaNotificationsDrill } from './HaNotificationsDrill'
import type { HaNotificationsResponse } from './types'

vi.mock('@/api/home_assistant')

function makeResult(
  overrides: Partial<UseQueryResult<HaNotificationsResponse, ApiError>>,
): UseQueryResult<HaNotificationsResponse, ApiError> {
  return {
    data: undefined,
    error: null,
    isPending: false,
    isError: false,
    isSuccess: false,
    isFetching: false,
    isLoading: false,
    isLoadingError: false,
    isRefetchError: false,
    isStale: false,
    isPlaceholderData: false,
    dataUpdatedAt: 0,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    fetchStatus: 'idle',
    refetch: vi.fn(),
    status: 'pending',
    ...overrides,
  } as UseQueryResult<HaNotificationsResponse, ApiError>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HaNotificationsDrill', () => {
  it('renders notification rows with title, message, and time', () => {
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(
      makeResult({
        data: {
          rows: [
            {
              notification_id: 'n1',
              title: 'Backup failed',
              message: 'The nightly backup did not complete.',
              created_at: '2026-01-01T00:00:00+00:00',
            },
          ],
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaNotificationsDrill />)
    expect(screen.getByText('Backup failed')).toBeInTheDocument()
    expect(screen.getByText('The nightly backup did not complete.')).toBeInTheDocument()
    // A <time> element renders for a valid created_at.
    expect(document.querySelector('time')).not.toBeNull()
  })

  it('falls back to (untitled) when title is null', () => {
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(
      makeResult({
        data: {
          rows: [
            {
              notification_id: 'n2',
              title: null,
              message: 'body',
              created_at: null,
            },
          ],
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaNotificationsDrill />)
    expect(screen.getByText('(untitled)')).toBeInTheDocument()
  })

  it('renders the empty label when there are no notifications', () => {
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(
      makeResult({
        data: { rows: [], returned: 0, total: 0 },
        isSuccess: true,
        status: 'success',
      }),
    )
    render(<HaNotificationsDrill />)
    expect(screen.getByText('No notifications')).toBeInTheDocument()
  })

  it('shows Loading… when pending', () => {
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(makeResult({ isPending: true }))
    render(<HaNotificationsDrill />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the 502 banner when the request returns 502', () => {
    const err = new Error('bad gateway') as ApiError & { status: number }
    err.status = 502
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(
      makeResult({ error: err, isError: true, status: 'error' }),
    )
    render(<HaNotificationsDrill />)
    expect(screen.getByText('Home Assistant metrics temporarily unavailable')).toBeInTheDocument()
  })

  it('renders malicious strings as inert literal text (no HTML/script/markdown)', () => {
    const maliciousMessage =
      '<script>window.__pwned=1</script>' +
      '<img src=x onerror="window.__pwned=1">' +
      '[click](javascript:alert(1))' +
      '**bold**'
    const maliciousTitle = "<script>document.title='x'</script>"
    vi.mocked(useHomeAssistantNotifications).mockReturnValue(
      makeResult({
        data: {
          rows: [
            {
              notification_id: 'evil',
              title: maliciousTitle,
              message: maliciousMessage,
              created_at: null,
            },
          ],
          returned: 1,
          total: 1,
        },
        isSuccess: true,
        status: 'success',
      }),
    )
    const { container } = render(<HaNotificationsDrill />)
    // No live DOM nodes created from the payload.
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    // The literal payload text is present (auto-escaped, rendered verbatim).
    expect(container.textContent).toContain(maliciousMessage)
    expect(container.textContent).toContain(maliciousTitle)
    // Markdown is NOT interpreted: raw markers appear literally.
    expect(container.textContent).toContain('**bold**')
    expect(container.textContent).toContain('[click](javascript:alert(1))')
  })
})
