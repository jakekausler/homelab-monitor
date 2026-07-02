import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ApiError } from '@/api/client'

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children, to, ...props }: { children: ReactNode; to: string; [key: string]: unknown }) =>
    React.createElement('a', { href: to, ...props }, children),
}))

vi.mock('@/api/runbooks', () => ({
  useRunbooks: vi.fn(),
  useToggleRunbook: vi.fn(),
  useRefreshRunbooks: vi.fn(),
  usePendingApprovals: vi.fn(),
  useApprovalPlan: vi.fn(),
  useApproveApproval: vi.fn(),
  useRejectApproval: vi.fn(),
  runbooksKeys: { all: ['runbooks'] },
  approvalsKeys: {
    all: ['autofix-approvals'],
    pending: ['autofix-approvals', 'pending'],
    plan: (id: string) => ['autofix-approvals', 'plan', id],
  },
}))

vi.mock('@/api/autofixSettings', () => ({
  useAutofixKillSwitch: vi.fn(),
  autofixSettingsKeys: { all: ['autofix-settings'] },
}))

import {
  useRunbooks,
  useToggleRunbook,
  useRefreshRunbooks,
  usePendingApprovals,
  useApprovalPlan,
  useApproveApproval,
  useRejectApproval,
  type Runbook,
} from '@/api/runbooks'
import { useAutofixKillSwitch } from '@/api/autofixSettings'
import { RunbooksPage } from '@/routes/runbooks/RunbooksPage'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const SAFE_RUNBOOK: Runbook = {
  id: 'runbook-safe',
  path: 'runbooks/pihole-restart-loop',
  created_at: '2026-01-01T00:00:00Z',
  alert_match_patterns: [{ alertname: 'PiholeRestartLoop' }],
  risk_tag: 'safe',
  dry_run_required: false,
  rate_limit_per_hour: 3,
  cooldown_seconds: 300,
  enabled: true,
  auto_trigger: false,
  content_hash: 'abc123',
}

const RISKY_RUNBOOK: Runbook = {
  id: 'runbook-risky',
  path: 'runbooks/nas-reboot',
  created_at: '2026-01-02T00:00:00Z',
  alert_match_patterns: [{ alertname: 'NasUnresponsive' }, { match: 'severity=critical' }],
  risk_tag: 'risky',
  dry_run_required: true,
  rate_limit_per_hour: 1,
  cooldown_seconds: 3600,
  enabled: false,
  auto_trigger: false,
  content_hash: 'def456',
}

function mockQuery<T>(
  overrides: Partial<{ data: T; isLoading: boolean; error: unknown }> = {},
): UseQueryResult<T, ApiError> {
  return {
    data: undefined as unknown as T,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as UseQueryResult<T, ApiError>
}

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

function setupDefaultMocks() {
  vi.mocked(useAutofixKillSwitch).mockReturnValue(
    mockQuery({ data: { enabled: true, updated_at: '2026-01-01T00:00:00Z' } }),
  )
  vi.mocked(useRunbooks).mockReturnValue(
    mockQuery({ data: { items: [SAFE_RUNBOOK, RISKY_RUNBOOK] } }),
  )
  vi.mocked(useToggleRunbook).mockReturnValue(mockMutation())
  vi.mocked(useRefreshRunbooks).mockReturnValue(mockMutation())
  vi.mocked(usePendingApprovals).mockReturnValue(mockQuery({ data: { items: [] } }))
  vi.mocked(useApprovalPlan).mockReturnValue(mockQuery() as never)
  vi.mocked(useApproveApproval).mockReturnValue(mockMutation())
  vi.mocked(useRejectApproval).mockReturnValue(mockMutation())
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RunbooksPage', () => {
  it('renders one card per runbook with risk badges and pattern chips', () => {
    setupDefaultMocks()
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('runbook-card-runbook-safe')).toBeInTheDocument()
    expect(screen.getByTestId('runbook-card-runbook-risky')).toBeInTheDocument()
    expect(screen.getByTestId('runbook-risk-badge-safe')).toBeInTheDocument()
    expect(screen.getByTestId('runbook-risk-badge-risky')).toBeInTheDocument()
    expect(screen.getByTestId('runbook-patterns-runbook-safe')).toBeInTheDocument()
  })

  it('renders dry-run-required badge only when required', () => {
    setupDefaultMocks()
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    // RISKY has dry_run_required=true, SAFE has false
    expect(screen.getAllByTestId('runbook-badge-dryrun')).toHaveLength(1)
  })

  it('Enabled toggle calls useToggleRunbook with correct body', () => {
    const mutate = vi.fn()
    setupDefaultMocks()
    vi.mocked(useToggleRunbook).mockReturnValue(mockMutation({ mutate }))
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('runbook-toggle-enabled-runbook-safe'))
    expect(mutate).toHaveBeenCalledWith({ id: 'runbook-safe', enabled: false })
  })

  it('Auto-trigger toggle on risky runbook renders info icon', () => {
    setupDefaultMocks()
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('runbook-risky-info-runbook-risky')).toBeInTheDocument()
    // Safe runbook has NO risky info icon
    expect(screen.queryByTestId('runbook-risky-info-runbook-safe')).not.toBeInTheDocument()
  })

  it('toggles are DISABLED when kill switch is off', () => {
    setupDefaultMocks()
    vi.mocked(useAutofixKillSwitch).mockReturnValue(
      mockQuery({ data: { enabled: false, updated_at: null } }),
    )
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    const enabledToggle = screen.getByTestId('runbook-toggle-enabled-runbook-safe')
    const autoToggle = screen.getByTestId('runbook-toggle-autotrigger-runbook-safe')
    expect(enabledToggle).toBeDisabled()
    expect(autoToggle).toBeDisabled()
  })

  it('Refresh button calls useRefreshRunbooks and shows summary banner', () => {
    const mutate = vi.fn()
    setupDefaultMocks()
    vi.mocked(useRefreshRunbooks).mockReturnValue(
      mockMutation({
        mutate,
        data: { registered: ['r-a'], refreshed: [], skipped: [], errors: [] },
      }) as never,
    )
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    fireEvent.click(screen.getByTestId('runbooks-refresh'))
    expect(mutate).toHaveBeenCalled()

    // Summary auto-renders based on refresh.data being present
    const summary = screen.getByTestId('runbooks-refresh-summary')
    expect(summary.textContent).toContain('Registered 1')
    expect(summary.textContent).toContain('refreshed 0')
    expect(summary.textContent).toContain('skipped 0')
    expect(summary.textContent).toContain('0 errors')
  })

  it('empty state renders when 0 runbooks', () => {
    setupDefaultMocks()
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ data: { items: [] } }))
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('runbooks-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('runbooks-catalog')).not.toBeInTheDocument()
  })

  it('kill-switch banner renders when kill switch off with settings link', () => {
    setupDefaultMocks()
    vi.mocked(useAutofixKillSwitch).mockReturnValue(
      mockQuery({ data: { enabled: false, updated_at: null } }),
    )
    render(<RunbooksPage />, { wrapper: makeWrapper() })

    const banner = screen.getByTestId('runbooks-killswitch-banner')
    expect(banner).toBeInTheDocument()
    expect(banner.textContent).toContain('Auto-fix is disabled')
    // Link is rendered via mocked <Link>
    const link = banner.querySelector('a')
    expect(link?.getAttribute('href')).toBe('/settings/autofix')
  })

  it('kill-switch banner absent when kill switch on', () => {
    setupDefaultMocks()
    render(<RunbooksPage />, { wrapper: makeWrapper() })
    expect(screen.queryByTestId('runbooks-killswitch-banner')).not.toBeInTheDocument()
  })

  it('loading state renders skeleton grid', () => {
    setupDefaultMocks()
    vi.mocked(useRunbooks).mockReturnValue(mockQuery({ isLoading: true }))
    render(<RunbooksPage />, { wrapper: makeWrapper() })
    expect(screen.getByTestId('runbooks-loading')).toBeInTheDocument()
  })

  it('refresh summary shows error details when refresh returns errors', () => {
    setupDefaultMocks()
    vi.mocked(useRefreshRunbooks).mockReturnValue(
      mockMutation({
        data: {
          registered: [],
          refreshed: ['runbook-a'],
          skipped: [],
          errors: [{ path: 'runbooks/bad', message: 'malformed yaml' }],
        },
        isSuccess: true,
      }),
    )
    render(<RunbooksPage />, { wrapper: makeWrapper() })
    // The summary + errors detail block should render
    expect(screen.getByTestId('runbooks-refresh-summary')).toBeInTheDocument()
    expect(screen.getByText(/malformed yaml/i)).toBeInTheDocument()
  })
})
