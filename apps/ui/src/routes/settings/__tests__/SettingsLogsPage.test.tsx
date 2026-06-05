// Project test conventions:
// - Vitest (explicit imports), vi.mock at top, QueryClientProvider wrapper
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { LogsRetentionResponse } from '@/api/settingsLogs'

vi.mock('@/api/settingsLogs', () => ({
  useLogsRetention: vi.fn(),
  useUpdateLogsRetention: vi.fn(),
}))

import { useLogsRetention, useUpdateLogsRetention } from '@/api/settingsLogs'
import { SettingsLogsPage } from '@/routes/settings/SettingsLogsPage'

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const BASE: LogsRetentionResponse = {
  retention_days: 30,
  pending_retention_days: null,
  disk_used_gb: 1.25,
  disk_used_pct: 12.5,
  disk_budget_available: true,
  warn_pct: 70,
  crit_pct: 85,
  retention_source: 'default',
  restart_required: false,
}

function mockQuery(
  overrides: Partial<ReturnType<typeof useLogsRetention>> = {},
): ReturnType<typeof useLogsRetention> {
  return {
    data: BASE,
    isLoading: false,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof useLogsRetention>
}

function mockMutation(
  overrides: Partial<ReturnType<typeof useUpdateLogsRetention>> = {},
): ReturnType<typeof useUpdateLogsRetention> {
  return {
    mutate: vi.fn(),
    isPending: false,
    ...overrides,
  } as unknown as ReturnType<typeof useUpdateLogsRetention>
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SettingsLogsPage', () => {
  it('renders effective retention + source + disk usage', () => {
    vi.mocked(useLogsRetention).mockReturnValue(mockQuery())
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('retention-current').textContent).toContain('30 days')
    expect(screen.getByTestId('retention-source').textContent).toBe('default')
    expect(screen.getByTestId('disk-used').textContent).toContain('1.25 GiB')
    expect(screen.getByTestId('disk-pct').textContent).toContain('12.5%')
    expect(screen.queryByTestId('restart-required-banner')).toBeNull()
    expect(screen.queryByTestId('retention-pending')).toBeNull()
  })

  it('shows pending + restart banner when restart_required', () => {
    vi.mocked(useLogsRetention).mockReturnValue(
      mockQuery({
        data: {
          ...BASE,
          pending_retention_days: 90,
          retention_source: 'runtime',
          restart_required: true,
        },
      }),
    )
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('retention-pending').textContent).toContain('90 days')
    expect(screen.getByTestId('restart-required-banner')).not.toBeNull()
  })

  it('seeds input from pending_retention_days when set', () => {
    vi.mocked(useLogsRetention).mockReturnValue(
      mockQuery({
        data: {
          ...BASE,
          pending_retention_days: 90,
          retention_source: 'runtime',
          restart_required: true,
        },
      }),
    )
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    const input = screen.getByTestId('retention-input')
    expect(input).toHaveProperty('value', '90')
  })

  it('shows disk unavailable message when disk_budget_available is false', () => {
    vi.mocked(useLogsRetention).mockReturnValue(
      mockQuery({
        data: {
          ...BASE,
          disk_budget_available: false,
        },
      }),
    )
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    expect(screen.getByTestId('disk-unavailable')).not.toBeNull()
    expect(screen.queryByTestId('disk-used')).toBeNull()
    expect(screen.queryByTestId('disk-pct')).toBeNull()
  })

  it('shows disk usage line (0.00 GiB / 0.0%) when budget_available is true but usage is zero', () => {
    vi.mocked(useLogsRetention).mockReturnValue(
      mockQuery({
        data: {
          ...BASE,
          disk_used_gb: 0,
          disk_used_pct: 0,
          disk_budget_available: true,
        },
      }),
    )
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    expect(screen.queryByTestId('disk-unavailable')).toBeNull()
    expect(screen.getByTestId('disk-used').textContent).toContain('0.00 GiB')
    expect(screen.getByTestId('disk-pct').textContent).toContain('0.0%')
  })

  it('Save calls mutate with the typed retention_days', () => {
    const mutate = vi.fn()
    vi.mocked(useLogsRetention).mockReturnValue(mockQuery())
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation({ mutate }))
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    const input = screen.getByTestId('retention-input')
    fireEvent.change(input, { target: { value: '90' } })
    fireEvent.click(screen.getByTestId('retention-save'))
    expect(mutate).toHaveBeenCalledWith({ retention_days: 90 })
  })

  it('clamps out-of-range input on save', () => {
    const mutate = vi.fn()
    vi.mocked(useLogsRetention).mockReturnValue(mockQuery())
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation({ mutate }))
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })

    fireEvent.change(screen.getByTestId('retention-input'), { target: { value: '99999' } })
    fireEvent.click(screen.getByTestId('retention-save'))
    expect(mutate).toHaveBeenCalledWith({ retention_days: 365 })
  })

  it('disables Save while pending', () => {
    vi.mocked(useLogsRetention).mockReturnValue(mockQuery())
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation({ isPending: true }))
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })
    const saveBtn = screen.getByTestId('retention-save')
    expect(saveBtn).toHaveProperty('disabled', true)
  })

  it('renders error state', () => {
    vi.mocked(useLogsRetention).mockReturnValue(
      mockQuery({ data: undefined, error: new Error('boom') as never }),
    )
    vi.mocked(useUpdateLogsRetention).mockReturnValue(mockMutation())
    render(<SettingsLogsPage />, { wrapper: makeWrapper() })
    expect(screen.getByTestId('settings-logs-page').textContent).toContain('Failed to load')
  })
})
