import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

import { RunbookCard } from '../RunbookCard'
import type { Runbook } from '@/api/runbooks'
import { useToggleRunbook } from '@/api/runbooks'
import type { ApiError } from '@/api/client'

vi.mock('@/api/runbooks', () => ({
  useToggleRunbook: vi.fn(),
}))

function mockMutation<TData = unknown, TVariables = unknown>(
  overrides: Record<string, unknown> = {},
): UseMutationResult<TData, ApiError, TVariables> {
  return {
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    error: null,
    data: undefined,
    reset: vi.fn(),
    ...overrides,
  } as unknown as UseMutationResult<TData, ApiError, TVariables>
}

const SAFE_RUNBOOK: Runbook = {
  id: 'runbook-safe',
  path: 'runbooks/safe-example',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [{ alertname: 'SafeAlert' }],
  risk_tag: 'safe',
  dry_run_required: false,
  rate_limit_per_hour: 5,
  cooldown_seconds: 300,
  enabled: true,
  auto_trigger: false,
  content_hash: 'hash-safe',
}

const RISKY_RUNBOOK: Runbook = {
  id: 'runbook-risky',
  path: 'runbooks/risky-example',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [{ alertname: 'RiskyAlert' }],
  risk_tag: 'risky',
  dry_run_required: true,
  rate_limit_per_hour: null,
  cooldown_seconds: null,
  enabled: false,
  auto_trigger: false,
  content_hash: 'hash-risky',
}

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children)
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RunbookCard', () => {
  it('renders safe runbook without dry-run badge and without risky info icon', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    render(<RunbookCard runbook={SAFE_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.getByTestId('runbook-card-runbook-safe')).toBeInTheDocument()
    expect(screen.queryByTestId('runbook-badge-dryrun')).not.toBeInTheDocument()
    expect(screen.queryByTestId('runbook-risky-info-runbook-safe')).not.toBeInTheDocument()
  })

  it('renders risky runbook with dry-run badge and risky info icon', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    render(<RunbookCard runbook={RISKY_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.getByTestId('runbook-badge-dryrun')).toBeInTheDocument()
    expect(screen.getByTestId('runbook-risky-info-runbook-risky')).toBeInTheDocument()
  })

  it('auto-trigger toggle calls mutation with auto_trigger key', () => {
    const mutate = vi.fn()
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunbookCard runbook={SAFE_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    fireEvent.click(screen.getByTestId('runbook-toggle-autotrigger-runbook-safe'))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'runbook-safe', auto_trigger: true }),
    )
  })

  it('copy config folder button calls clipboard.writeText', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    render(<RunbookCard runbook={SAFE_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    fireEvent.click(screen.getByTestId('runbook-copy-path-runbook-safe'))
    expect(writeText).toHaveBeenCalledWith('runbooks/safe-example')
  })

  it('copy config folder swallows clipboard errors silently', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    const writeText = vi.fn().mockRejectedValue(new Error('permission denied'))
    Object.assign(navigator, { clipboard: { writeText } })
    render(<RunbookCard runbook={SAFE_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    // Should not throw
    fireEvent.click(screen.getByTestId('runbook-copy-path-runbook-safe'))
    expect(writeText).toHaveBeenCalled()
  })

  it('renders rate limit and cooldown summary when set', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    render(<RunbookCard runbook={SAFE_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    // SAFE_RUNBOOK has rate_limit_per_hour: 5 and cooldown_seconds: 300
    expect(screen.getByText(/5\s*\/\s*hr/i)).toBeInTheDocument()
  })

  it('omits rate limit summary when both fields are null', () => {
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
    render(<RunbookCard runbook={RISKY_RUNBOOK} killSwitchEnabled={true} />, {
      wrapper: makeWrapper(),
    })
    expect(screen.queryByText(/\/\s*hr/i)).not.toBeInTheDocument()
  })
})
