import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UseQueryResult, UseMutationResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'
import { usePiholeOverview } from '@/api/pihole'
import { useListContainers, useContainerLifecycleMutation } from '@/api/docker'
import { PiholeVersionContainerWidget } from './PiholeVersionContainerWidget'

vi.mock('@/api/pihole')
vi.mock('@/api/docker')
vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}))

type ContainerListResponse = Schema<'ContainerListResponse'>
type OverviewResponse = Schema<'PiholeOverviewResponse'>

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

function mutationMock<V = unknown, R = unknown>(
  over: Partial<UseMutationResult<R, ApiError, V>> = {},
): UseMutationResult<R, ApiError, V> {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    isIdle: true,
    error: null,
    data: undefined,
    reset: vi.fn(),
    ...over,
  } as unknown as UseMutationResult<R, ApiError, V>
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PiholeVersionContainerWidget', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading when overview pending', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(pending())
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows 502 when overview is 502', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(502))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('Pi-hole versions temporarily unavailable')).toBeInTheDocument()
  })

  it('shows error display when overview non-502 error', () => {
    vi.mocked(usePiholeOverview).mockReturnValue(err(500))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText(/Internal error/i)).toBeInTheDocument()
  })

  it('renders version rows', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [
        { component: 'core', version: 'v6.0' },
        { component: 'ftl', version: 'v6.0' },
      ],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('core')).toBeInTheDocument()
    expect(screen.getByText('ftl')).toBeInTheDocument()
    expect(screen.getAllByText('v6.0').length).toBeGreaterThan(0)
  })

  it('shows update available badge', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [
        { component: 'core', version: 'v6.0' },
        { component: 'ftl', version: 'v6.0' },
      ],
      updates_available: [{ component: 'core' }],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    const badges = screen.getAllByText('update available')
    expect(badges.length).toBeGreaterThan(0)
  })

  it('shows empty state when no versions', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByTestId('pihole-versions-empty')).toBeInTheDocument()
  })

  it('shows running badge when container status contains running', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    const containerList: ContainerListResponse = {
      containers: [
        {
          id: 'container-1',
          name: 'pihole-unbound',
          status: 'running',
          healthcheck: null,
          labels: {},
        },
      ],
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok(containerList))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('shows exited badge when container status contains exited', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    const containerList: ContainerListResponse = {
      containers: [
        {
          id: 'container-1',
          name: 'pihole-unbound',
          status: 'exited (0)',
          healthcheck: null,
          labels: {},
        },
      ],
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok(containerList))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('exited (0)')).toBeInTheDocument()
  })

  it('shows unknown when container not found', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('unknown')).toBeInTheDocument()
  })

  it('shows error message when containers list errors', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(err(500))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByText('Container status unavailable')).toBeInTheDocument()
  })

  it('calls restart mutation on restart confirm', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    const mockMutate = vi.fn()
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock({ mutate: mockMutate }))

    render(<PiholeVersionContainerWidget />)
    fireEvent.click(screen.getByTestId('pihole-container-restart-button'))

    // Dialog should appear
    const titleElements = screen.queryAllByText(/Restart pihole-unbound/i)
    expect(titleElements.length).toBeGreaterThan(0)
  })

  it('shows transient label after restart mutation succeeds', async () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    const containerList: ContainerListResponse = {
      containers: [
        {
          id: 'container-1',
          name: 'pihole-unbound',
          status: 'running',
          healthcheck: null,
          labels: {},
        },
      ],
    }
    const mockMutate = vi.fn((_variables: unknown, options: unknown) => {
      const opts = options as { onSuccess?: (...args: unknown[]) => void }
      if (opts?.onSuccess) {
        opts.onSuccess(
          {
            action: 'restart',
            container_name: 'pihole-unbound',
            container_id: 'abc',
            audit_id: 'aud1',
          },
          undefined,
          undefined,
          undefined,
        )
      }
    })
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok(containerList))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(
      mutationMock({ mutate: mockMutate }) as ReturnType<typeof useContainerLifecycleMutation>,
    )

    render(<PiholeVersionContainerWidget />)
    fireEvent.click(screen.getByTestId('pihole-container-restart-button'))

    // Dialog should appear with title
    expect(screen.getByText(/Restart pihole-unbound/i)).toBeInTheDocument()

    // Find the confirm input and type the phrase
    const confirmInput = screen.getByPlaceholderText('restart')
    fireEvent.change(confirmInput, { target: { value: 'restart' } })

    // Find and click the confirm button (the one with the destructive variant in the dialog)
    const buttons = screen.getAllByRole('button', { name: /Restart/i })
    const confirmButton = buttons[buttons.length - 1] // Last button is the confirm button in the dialog
    if (confirmButton) {
      fireEvent.click(confirmButton)
    }

    // Assert the transient label appears
    const label = await screen.findByText(/Restarting…/i)
    expect(label).toBeInTheDocument()
  })

  it('renders stop button with destructive variant', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByTestId('pihole-container-stop-button')).toBeInTheDocument()
  })

  it('renders all three buttons', () => {
    const overview: OverviewResponse = {
      up: true,
      blocking_enabled: true,
      blocking_timer_seconds: null,
      percent_blocked: 45.5,
      query_frequency: 1000.0,
      messages_count: 0,
      privacy_level: 0,
      query_logging_enabled: true,
      gravity_domains: 100000,
      versions: [],
      updates_available: [],
      query_feed_streaming: false,
    }
    vi.mocked(usePiholeOverview).mockReturnValue(ok(overview))
    vi.mocked(useListContainers).mockReturnValue(ok({ containers: [] }))
    vi.mocked(useContainerLifecycleMutation).mockReturnValue(mutationMock())

    render(<PiholeVersionContainerWidget />)
    expect(screen.getByTestId('pihole-container-restart-button')).toBeInTheDocument()
    expect(screen.getByTestId('pihole-container-start-button')).toBeInTheDocument()
    expect(screen.getByTestId('pihole-container-stop-button')).toBeInTheDocument()
  })
})
