import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Stub CreateAlertModal to avoid CodeMirror/jsdom issues and to assert editRuleId propagation.
vi.mock('@/components/logs/CreateAlertModal', () => ({
  CreateAlertModal: vi.fn(
    ({
      open,
      editRuleId,
    }: {
      open: boolean
      editRuleId?: number
      onOpenChange: (v: boolean) => void
    }) =>
      open ? (
        <div data-testid="create-alert-modal-stub" data-edit-rule-id={editRuleId ?? ''}>
          {editRuleId !== undefined ? 'Edit alert rule' : 'Create alert from query'}
        </div>
      ) : null,
  ),
}))

vi.mock('@/api/userRules', () => ({
  useUserRules: vi.fn(),
  useUserRulesHealth: vi.fn(),
  useEnableUserRule: vi.fn(),
  useDisableUserRule: vi.fn(),
  useDeleteUserRule: vi.fn(),
}))

import {
  useUserRules,
  useUserRulesHealth,
  useEnableUserRule,
  useDisableUserRule,
  useDeleteUserRule,
} from '@/api/userRules'
import { UserRulesTab } from '../UserRulesTab'

const mockUseUserRules = vi.mocked(useUserRules)
const mockUseUserRulesHealth = vi.mocked(useUserRulesHealth)
const mockUseEnableUserRule = vi.mocked(useEnableUserRule)
const mockUseDisableUserRule = vi.mocked(useDisableUserRule)
const mockUseDeleteUserRule = vi.mocked(useDeleteUserRule)

// Minimal rule fixture factory.
function makeRule(overrides: {
  id: number
  rule_name: string
  expr?: string
  expr_kind?: 'logsql' | 'metricsql'
  severity?: 'info' | 'warning' | 'critical'
  enabled?: boolean
  summary?: string
  description?: string
  for_duration?: string
}) {
  return {
    id: overrides.id,
    rule_name: overrides.rule_name,
    expr: overrides.expr ?? 'service:foo | stats count() as c | filter c:>1',
    expr_kind: overrides.expr_kind ?? ('logsql' as const),
    severity: overrides.severity ?? ('warning' as const),
    enabled: overrides.enabled ?? true,
    summary: overrides.summary ?? 'Test alert',
    description: overrides.description ?? '',
    for_duration: overrides.for_duration ?? '5m',
  }
}

function defaultMutations() {
  mockUseEnableUserRule.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as never)
  mockUseDisableUserRule.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as never)
  mockUseDeleteUserRule.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as never)
}

afterEach(() => {
  cleanup()
})

describe('UserRulesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUseUserRulesHealth.mockReturnValue({ data: { rules: {} } } as never)
    defaultMutations()
  })

  it('shows empty state when no rules exist', () => {
    mockUseUserRules.mockReturnValue({
      data: { rules: [] },
      isLoading: false,
      error: null,
    } as never)
    render(<UserRulesTab />)
    expect(screen.getByTestId('user-rules-empty')).toBeInTheDocument()
    expect(screen.getByText('No alert rules yet.')).toBeInTheDocument()
  })

  it('renders a row for each rule with name, expr_kind, severity, and enabled state', () => {
    const rules = [
      makeRule({
        id: 1,
        rule_name: 'HighErrorRate',
        expr_kind: 'logsql',
        severity: 'critical',
        enabled: true,
      }),
      makeRule({
        id: 2,
        rule_name: 'DiskFull',
        expr_kind: 'metricsql',
        severity: 'warning',
        enabled: false,
      }),
    ]
    mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)
    render(<UserRulesTab />)

    const rows = screen.getAllByTestId('user-rules-row')
    // Two rows in the desktop table + two in the mobile cards = 4 total.
    expect(rows.length).toBeGreaterThanOrEqual(2)

    // Desktop table row 1.
    const desktopTable = screen.getByTestId('user-rules-table')
    const tableRows = within(desktopTable).getAllByTestId('user-rules-row')
    expect(tableRows).toHaveLength(2)

    const row1 = tableRows[0] as HTMLElement
    expect(within(row1).getByText('HighErrorRate')).toBeInTheDocument()
    expect(within(row1).getByText('logsql')).toBeInTheDocument()
    expect(within(row1).getByText('critical')).toBeInTheDocument()
    expect(within(row1).getByText('on')).toBeInTheDocument()

    const row2 = tableRows[1] as HTMLElement
    expect(within(row2).getByText('DiskFull')).toBeInTheDocument()
    expect(within(row2).getByText('metricsql')).toBeInTheDocument()
    expect(within(row2).getByText('warning')).toBeInTheDocument()
    expect(within(row2).getByText('off')).toBeInTheDocument()
  })

  describe('health join', () => {
    it('shows "ok" badge with data-health=ok for a rule with ok health', () => {
      const rules = [makeRule({ id: 1, rule_name: 'OkRule' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)
      mockUseUserRulesHealth.mockReturnValue({
        data: { rules: { OkRule: { health: 'ok', last_error: '' } } },
      } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const healthBadges = within(desktopTable).getAllByTestId('user-rule-health')
      expect(healthBadges[0]).toHaveAttribute('data-health', 'ok')
      expect(healthBadges[0]).toHaveTextContent('ok')
    })

    it('shows "err" badge with lastError in title for a rule with err health', () => {
      const rules = [makeRule({ id: 1, rule_name: 'BrokenRule' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)
      mockUseUserRulesHealth.mockReturnValue({
        data: {
          rules: { BrokenRule: { health: 'err', last_error: 'syntax error at position 5' } },
        },
      } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const healthBadges = within(desktopTable).getAllByTestId('user-rule-health')
      expect(healthBadges[0]).toHaveAttribute('data-health', 'err')
      expect(healthBadges[0]).toHaveAttribute('title', 'syntax error at position 5')
      expect(healthBadges[0]).toHaveTextContent('err')
    })

    it('shows "unknown" badge for a rule absent from the health map', () => {
      const rules = [makeRule({ id: 1, rule_name: 'MissingFromHealth' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)
      // Health map deliberately empty — rule not loaded by vmalert yet.
      mockUseUserRulesHealth.mockReturnValue({ data: { rules: {} } } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const healthBadges = within(desktopTable).getAllByTestId('user-rule-health')
      expect(healthBadges[0]).toHaveAttribute('data-health', 'unknown')
      expect(healthBadges[0]).toHaveTextContent('unknown')
    })
  })

  describe('enable/disable toggle', () => {
    it('calls disableMut.mutate with the rule id when the rule is enabled', () => {
      const disableMutFn = vi.fn()
      mockUseDisableUserRule.mockReturnValue({ mutate: disableMutFn, isPending: false } as never)
      const rules = [makeRule({ id: 42, rule_name: 'ActiveRule', enabled: true })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      // Desktop table toggle button.
      const desktopTable = screen.getByTestId('user-rules-table')
      const toggleBtn = within(desktopTable).getAllByTestId('user-rule-toggle')[0] as HTMLElement
      expect(toggleBtn).toHaveTextContent('Disable')
      fireEvent.click(toggleBtn)

      expect(disableMutFn).toHaveBeenCalledWith(42)
      expect(vi.mocked(useEnableUserRule)().mutate).not.toHaveBeenCalled()
    })

    it('calls enableMut.mutate with the rule id when the rule is disabled', () => {
      const enableMutFn = vi.fn()
      mockUseEnableUserRule.mockReturnValue({ mutate: enableMutFn, isPending: false } as never)
      const rules = [makeRule({ id: 7, rule_name: 'InactiveRule', enabled: false })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const toggleBtn = within(desktopTable).getAllByTestId('user-rule-toggle')[0] as HTMLElement
      expect(toggleBtn).toHaveTextContent('Enable')
      fireEvent.click(toggleBtn)

      expect(enableMutFn).toHaveBeenCalledWith(7)
    })
  })

  describe('delete confirm', () => {
    it('calls deleteMut.mutate with the rule id when window.confirm returns true', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
      const deleteMutFn = vi.fn()
      mockUseDeleteUserRule.mockReturnValue({ mutate: deleteMutFn, isPending: false } as never)
      const rules = [makeRule({ id: 99, rule_name: 'DeleteMe' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const deleteBtn = within(desktopTable).getAllByTestId('user-rule-delete')[0] as HTMLElement
      fireEvent.click(deleteBtn)

      expect(confirmSpy).toHaveBeenCalled()
      expect(deleteMutFn).toHaveBeenCalledWith(99)
      confirmSpy.mockRestore()
    })

    it('does NOT call deleteMut.mutate when window.confirm returns false', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
      const deleteMutFn = vi.fn()
      mockUseDeleteUserRule.mockReturnValue({ mutate: deleteMutFn, isPending: false } as never)
      const rules = [makeRule({ id: 99, rule_name: 'DeleteMe' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      const deleteBtn = within(desktopTable).getAllByTestId('user-rule-delete')[0] as HTMLElement
      fireEvent.click(deleteBtn)

      expect(confirmSpy).toHaveBeenCalled()
      expect(deleteMutFn).not.toHaveBeenCalled()
      confirmSpy.mockRestore()
    })
  })

  describe('edit relaunch', () => {
    it('opens the CreateAlertModal stub with the correct editRuleId when Edit is clicked', () => {
      const rules = [makeRule({ id: 55, rule_name: 'EditableRule' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      // Modal should not be visible before clicking Edit.
      expect(screen.queryByTestId('create-alert-modal-stub')).not.toBeInTheDocument()

      const desktopTable = screen.getByTestId('user-rules-table')
      const editBtn = within(desktopTable).getAllByTestId('user-rule-edit')[0] as HTMLElement
      fireEvent.click(editBtn)

      const modalStub = screen.getByTestId('create-alert-modal-stub')
      expect(modalStub).toBeInTheDocument()
      expect(modalStub).toHaveAttribute('data-edit-rule-id', '55')
      expect(modalStub).toHaveTextContent('Edit alert rule')
    })

    it('closes the CreateAlertModal stub and clears editRuleId when onOpenChange(false) is called', async () => {
      const { CreateAlertModal: MockModal } = await import('@/components/logs/CreateAlertModal')
      const rules = [makeRule({ id: 55, rule_name: 'EditableRule' })]
      mockUseUserRules.mockReturnValue({ data: { rules }, isLoading: false, error: null } as never)

      render(<UserRulesTab />)

      const desktopTable = screen.getByTestId('user-rules-table')
      fireEvent.click(within(desktopTable).getAllByTestId('user-rule-edit')[0] as HTMLElement)

      // Simulate the modal calling onOpenChange(false).
      const lastCallProps = vi.mocked(MockModal).mock.calls.at(-1)?.[0]
      act(() => {
        lastCallProps?.onOpenChange(false)
      })

      expect(screen.queryByTestId('create-alert-modal-stub')).not.toBeInTheDocument()
    })
  })

  it('shows loading state while rules are being fetched', () => {
    mockUseUserRules.mockReturnValue({ data: undefined, isLoading: true, error: null } as never)
    render(<UserRulesTab />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows error message when the rules query fails', () => {
    mockUseUserRules.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('api error'),
    } as never)
    render(<UserRulesTab />)
    expect(screen.getByText('Error loading alert rules')).toBeInTheDocument()
  })
})
