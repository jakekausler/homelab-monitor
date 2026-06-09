import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ApiError } from '@/api/client'
import { toast } from 'sonner'

// Force the textarea fallback for both LogsQlEditor and YamlPreview (CodeMirror
// is non-functional in jsdom; mirror LogsQlEditor.test.tsx).
vi.mock('@/lib/useMediaQuery', () => ({
  useMediaQuery: vi.fn(() => false),
}))

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('@/api/userRules', () => ({
  useUserRules: vi.fn(),
  useCreateUserRule: vi.fn(),
  usePatchUserRule: vi.fn(),
}))

vi.mock('@/api/logs', () => ({
  useLogsServicesQuery: vi.fn(),
}))

import {
  CreateAlertModal,
  scaffoldLogsqlExpr,
  buildSimpleExpr,
  advancedExprWarnings,
} from '../CreateAlertModal'
import { useUserRules, useCreateUserRule, usePatchUserRule } from '@/api/userRules'
import { useLogsServicesQuery } from '@/api/logs'

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const baseInitial = {
  rule_name: 'MyRule',
  expr: 'service:foo | stats count() as match_count | filter match_count:>10',
  summary: 'something happened',
}

describe('CreateAlertModal', () => {
  beforeEach(() => {
    vi.useRealTimers()
    cleanup()
    vi.clearAllMocks()
    vi.mocked(useUserRules).mockReturnValue({ data: { rules: [] } } as never)
    vi.mocked(useCreateUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(usePatchUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: { services: [], truncated: false },
    } as never)
  })

  it('renders when open with seeded values', () => {
    renderWithClient(<CreateAlertModal open onOpenChange={vi.fn()} initialValues={baseInitial} />)
    expect(screen.getByTestId('create-alert-modal')).toBeInTheDocument()
    expect(screen.getByTestId('create-alert-name')).toHaveValue('MyRule')
  })

  it('updates the YAML preview when a field changes (debounced)', async () => {
    vi.useFakeTimers()
    try {
      renderWithClient(
        <CreateAlertModal
          open
          onOpenChange={vi.fn()}
          initialMode="advanced"
          initialValues={baseInitial}
        />,
      )
      fireEvent.change(screen.getByTestId('create-alert-name'), { target: { value: 'NewName' } })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(350)
      })
      const preview = screen.getByTestId('yaml-preview-textarea')
      expect((preview as HTMLTextAreaElement).value).toContain('alert: NewName')
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows a regex error for an invalid rule_name on submit', async () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    fireEvent.change(screen.getByTestId('create-alert-name'), { target: { value: '1bad' } })
    fireEvent.click(screen.getByTestId('create-alert-submit'))
    expect(
      await screen.findByText('Letters, digits, underscore; must not start with a digit'),
    ).toBeInTheDocument()
  })

  it('shows a for_duration error on submit for a bad duration', async () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    fireEvent.change(screen.getByTestId('create-alert-for'), { target: { value: 'banana' } })
    fireEvent.click(screen.getByTestId('create-alert-submit'))
    expect(
      await screen.findByText('Use durations like 5m, 30s, 1h, 2h30m, or 0s'),
    ).toBeInTheDocument()
  })

  it('shows inline uniqueness error when the name matches an existing rule', async () => {
    vi.mocked(useUserRules).mockReturnValue({
      data: { rules: [{ id: 1, rule_name: 'Taken' }] },
    } as never)
    vi.useFakeTimers()
    try {
      renderWithClient(
        <CreateAlertModal
          open
          onOpenChange={vi.fn()}
          initialMode="advanced"
          initialValues={baseInitial}
        />,
      )
      fireEvent.change(screen.getByTestId('create-alert-name'), { target: { value: 'Taken' } })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(350)
      })
      // The debounced conflict state has flushed synchronously under fake timers;
      // assert with a sync query so findBy* polling never switches timer modes.
      expect(screen.getByTestId('create-alert-name-conflict')).toHaveTextContent(
        'A rule with that name already exists',
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('submits successfully → toast.success + onOpenChange(false)', async () => {
    const onOpenChange = vi.fn()
    const mutate = vi.fn().mockResolvedValue({})
    vi.mocked(useCreateUserRule).mockReturnValue({ mutateAsync: mutate, isPending: false } as never)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={onOpenChange}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    await waitFor(() => expect(mutate).toHaveBeenCalled())
    // The POST body must NOT include source_kind/source_ref or simple_* fields.
    const body = mutate.mock.calls[0]![0] as Record<string, unknown>
    expect(body).not.toHaveProperty('source_kind')
    expect(body).not.toHaveProperty('source_ref')
    expect(body).not.toHaveProperty('simple_service')
    expect(body).not.toHaveProperty('simple_contains')
    expect(body).not.toHaveProperty('simple_threshold')
    expect(body).not.toHaveProperty('simple_window')
    expect(body.rule_name).toBe('MyRule')
    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('on 409 shows inline name error and keeps modal open', async () => {
    const onOpenChange = vi.fn()
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'conflict',
        message: 'rule_name already exists: MyRule',
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useCreateUserRule).mockReturnValue({ mutateAsync: mutate, isPending: false } as never)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={onOpenChange}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    // Wrap the submit click in async act() so the FULL async chain
    // (zod resolver → mutateAsync rejection → catch → setNameError → re-render)
    // flushes inside the act boundary for the common (fast) case. The act flush
    // handles a single microtask hop, but the rejection chain crosses several
    // promise hops; under full-suite microtask/GC contention the setState +
    // re-render may not have committed by the time act() returns. waitFor
    // retries on a real-timer interval (default 1000ms window), giving the
    // multi-hop chain bounded time to commit. A bare synchronous getByTestId
    // has ZERO retry tolerance and raced under full-suite load.
    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(screen.getByTestId('create-alert-name-conflict')).toBeInTheDocument()
    })
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('on 400 invalid_expr shows inline expr error and keeps modal open', async () => {
    const onOpenChange = vi.fn()
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 400,
        code: 'invalid_expr',
        message: 'Log alert expressions need a | stats ... pipe.',
        retryAfterSeconds: null,
        details: { check: 'missing_stats_pipe' },
      }),
    )
    vi.mocked(useCreateUserRule).mockReturnValue({ mutateAsync: mutate, isPending: false } as never)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={onOpenChange}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    // waitFor (not a bare getByTestId) so the multi-hop reject→catch→setState
    // chain has bounded time to commit under full-suite contention.
    await waitFor(() => {
      expect(screen.getByTestId('create-alert-expr-error')).toHaveTextContent('| stats')
    })
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('on 400 invalid_rule shows inline form error and keeps modal open', async () => {
    const onOpenChange = vi.fn()
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 400,
        code: 'invalid_rule',
        message: "for_duration must match '^(\\d+[smhd])+$|^0s$', got 'invalid'",
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useCreateUserRule).mockReturnValue({ mutateAsync: mutate, isPending: false } as never)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={onOpenChange}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    // waitFor (not a bare getByTestId) so the multi-hop reject→catch→setState
    // chain has bounded time to commit under full-suite contention.
    await waitFor(() => {
      expect(screen.getByTestId('create-alert-form-error')).toHaveTextContent(
        'for_duration must match',
      )
    })
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('on non-409 ApiError shows toast.error and stays open', async () => {
    const onOpenChange = vi.fn()
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 500,
        code: 'internal',
        message: 'boom',
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useCreateUserRule).mockReturnValue({ mutateAsync: mutate, isPending: false } as never)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={onOpenChange}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )
    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('boom'))
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })
})

describe('advancedExprWarnings', () => {
  it('valid logsql expr with stats and filter alias returns no warnings', () => {
    const expr = '_msg:error | stats count() as match_count | filter match_count:>0'
    expect(advancedExprWarnings(expr)).toEqual([])
  })

  it('missing stats pipe returns a warning', () => {
    const expr = '_msg:error'
    const warnings = advancedExprWarnings(expr)
    expect(warnings.some((w) => /stats/.test(w))).toBe(true)
  })

  it('reserved filter field returns a warning', () => {
    const expr = '_msg:error | stats count() as c | filter count:>0'
    const warnings = advancedExprWarnings(expr)
    expect(warnings.some((w) => /reserved/i.test(w))).toBe(true)
  })

  it('unbalanced double-quotes returns a warning', () => {
    const expr = '_msg:"unterminated | stats count() as c | filter c:>0'
    const warnings = advancedExprWarnings(expr)
    expect(warnings.some((w) => /quote/.test(w))).toBe(true)
  })

  it('empty string returns no warnings', () => {
    expect(advancedExprWarnings('')).toEqual([])
  })

  it('alias with reserved substring (oom_count) is not flagged as reserved', () => {
    const expr = '... | stats count() as oom_count | filter oom_count:>0'
    const warnings = advancedExprWarnings(expr)
    expect(warnings.some((w) => /reserved/i.test(w))).toBe(false)
  })
})

describe('buildSimpleExpr', () => {
  it('builds with service + contains + threshold + window', () => {
    expect(
      buildSimpleExpr({ service: 'homeassistant', contains: 'ERROR', threshold: 10, window: '5m' }),
    ).toBe(
      '_time:5m service:="homeassistant" "ERROR" | stats count() as match_count | filter match_count:>10',
    )
  })

  it('omits the service filter when service is empty', () => {
    expect(buildSimpleExpr({ service: '', contains: 'boom', threshold: 5, window: '1h' })).toBe(
      '_time:1h "boom" | stats count() as match_count | filter match_count:>5',
    )
  })

  it('omits the contains filter when contains is empty', () => {
    expect(buildSimpleExpr({ service: 'sshd', contains: '', threshold: 3, window: '15m' })).toBe(
      '_time:15m service:="sshd" | stats count() as match_count | filter match_count:>3',
    )
  })

  it('omits both filters when service and contains are empty', () => {
    expect(buildSimpleExpr({ service: '', contains: '', threshold: 1, window: '30s' })).toBe(
      '_time:30s | stats count() as match_count | filter match_count:>1',
    )
  })

  it('escapes double quotes in contains', () => {
    expect(
      buildSimpleExpr({ service: '', contains: 'said "hi"', threshold: 2, window: '5m' }),
    ).toBe('_time:5m "said \\"hi\\"" | stats count() as match_count | filter match_count:>2')
  })

  it('escapes backslashes (backslash first) and braces in contains', () => {
    const out = buildSimpleExpr({
      service: '',
      contains: 'path C:\\temp {brace}',
      threshold: 2,
      window: '5m',
    })
    // backslash doubled, braces pass through literally inside the quoted phrase
    expect(out).toBe(
      '_time:5m "path C:\\\\temp {brace}" | stats count() as match_count | filter match_count:>2',
    )
  })

  it('escapes quotes in the service value too', () => {
    expect(buildSimpleExpr({ service: 'we"ird', contains: '', threshold: 1, window: '5m' })).toBe(
      '_time:5m service:="we\\"ird" | stats count() as match_count | filter match_count:>1',
    )
  })
})

describe('CreateAlertModal modes', () => {
  beforeEach(() => {
    vi.useRealTimers()
    cleanup()
    vi.clearAllMocks()
    vi.mocked(useUserRules).mockReturnValue({ data: { rules: [] } } as never)
    vi.mocked(useCreateUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(usePatchUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: { services: [], truncated: false },
    } as never)
  })

  it('defaults to Simple mode and shows the structured fields', () => {
    renderWithClient(<CreateAlertModal open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('create-alert-simple-fields')).toBeInTheDocument()
    expect(screen.queryByTestId('create-alert-advanced-fields')).not.toBeInTheDocument()
    // "Any service" option present even with empty services list.
    expect(screen.getByRole('option', { name: 'Any service' })).toBeInTheDocument()
  })

  it('recompiles expr into the YAML preview when Simple fields change', async () => {
    vi.useFakeTimers()
    try {
      renderWithClient(<CreateAlertModal open onOpenChange={vi.fn()} />)
      fireEvent.change(screen.getByTestId('create-alert-contains'), {
        target: { value: 'Out of memory' },
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(350)
      })
      const preview = screen.getByTestId('yaml-preview-textarea')
      expect((preview as HTMLTextAreaElement).value).toContain('"Out of memory"')
      expect((preview as HTMLTextAreaElement).value).toContain(
        '| stats count() as match_count | filter match_count:>10',
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('opens in Advanced mode when initialMode=advanced and shows the expr editor + doc link', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{
          expr: 'service:foo | stats count() as match_count | filter match_count:>1',
        }}
      />,
    )
    expect(screen.getByTestId('create-alert-advanced-fields')).toBeInTheDocument()
    expect(screen.getByTestId('create-alert-logsql-doc-link')).toHaveAttribute(
      'href',
      'https://docs.victoriametrics.com/victorialogs/logsql/',
    )
  })

  it('shows the expr advisory warnings for an Advanced logsql expr without a stats pipe', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{ expr: 'service:foo' }}
      />,
    )
    expect(screen.getByTestId('create-alert-expr-warnings')).toBeInTheDocument()
  })

  it('does NOT show expr warnings when the expr has a stats pipe', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{ expr: 'service:foo | stats count() as c' }}
      />,
    )
    expect(screen.queryByTestId('create-alert-expr-warnings')).not.toBeInTheDocument()
  })

  it('Advanced→Simple confirms when the expr was hand-edited and recompiles on confirm', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{ expr: 'service:foo | stats count() as c' }}
      />,
    )
    // hand-edit the expr (textarea fallback because useMediaQuery mocked false)
    fireEvent.change(screen.getByTestId('logsql-editor-textarea'), {
      target: { value: 'service:bar | stats count() as c' },
    })
    fireEvent.click(screen.getByTestId('create-alert-mode-simple'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(screen.getByTestId('create-alert-simple-fields')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('Advanced→Simple does NOT confirm when the expr was not hand-edited', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{ expr: 'service:foo | stats count() as c' }}
      />,
    )
    fireEvent.click(screen.getByTestId('create-alert-mode-simple'))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('create-alert-simple-fields')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('Advanced→Simple stays in Advanced if the user cancels the confirm', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={{ expr: 'service:foo | stats count() as c' }}
      />,
    )
    fireEvent.change(screen.getByTestId('logsql-editor-textarea'), {
      target: { value: 'edited | stats count() as c' },
    })
    fireEvent.click(screen.getByTestId('create-alert-mode-simple'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(screen.getByTestId('create-alert-advanced-fields')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('populates the service dropdown from useLogsServicesQuery', () => {
    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: {
        services: [{ service: 'nginx', source_type: 'docker', count: 42 }],
        truncated: false,
      },
    } as never)
    renderWithClient(<CreateAlertModal open onOpenChange={vi.fn()} />)
    expect(screen.getByRole('option', { name: /nginx/ })).toBeInTheDocument()
  })
})

describe('scaffoldLogsqlExpr', () => {
  it('wraps a bare query with a count threshold pipe', () => {
    expect(scaffoldLogsqlExpr('service:foo')).toBe(
      'service:foo | stats count() as match_count | filter match_count:>10',
    )
  })
  it('does NOT double-wrap a query already containing | stats', () => {
    const q = 'service:foo | stats count() as c'
    expect(scaffoldLogsqlExpr(q)).toBe(q)
  })
  it('matches |stats without spaces (case-insensitive)', () => {
    const q = 'service:foo |STATS count()'
    expect(scaffoldLogsqlExpr(q)).toBe(q)
  })
  it('returns empty string for empty/whitespace input', () => {
    expect(scaffoldLogsqlExpr('   ')).toBe('')
  })
})

// --- Edit mode (STAGE-004-043A) ---

const editInitial = {
  rule_name: 'ExistingRule',
  expr: 'service:foo | stats count() as match_count | filter match_count:>5',
  expr_kind: 'logsql' as const,
  severity: 'warning' as const,
  for_duration: '5m',
  summary: 'Existing summary',
  description: 'Existing description',
}

describe('CreateAlertModal edit mode', () => {
  beforeEach(() => {
    vi.useRealTimers()
    cleanup()
    vi.clearAllMocks()
    vi.mocked(useUserRules).mockReturnValue({ data: { rules: [] } } as never)
    vi.mocked(useCreateUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(usePatchUserRule).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({}),
      isPending: false,
    } as never)
    vi.mocked(useLogsServicesQuery).mockReturnValue({
      data: { services: [], truncated: false },
    } as never)
  })

  it('shows "Edit alert rule" as the dialog title in edit mode', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        editRuleId={1}
        initialValues={editInitial}
        initialMode="advanced"
      />,
    )
    expect(screen.getByText('Edit alert rule')).toBeInTheDocument()
  })

  it('shows "Save changes" as the submit button label in edit mode', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        editRuleId={1}
        initialValues={editInitial}
        initialMode="advanced"
      />,
    )
    expect(screen.getByTestId('create-alert-submit')).toHaveTextContent('Save changes')
  })

  it('rule_name input is disabled in edit mode', () => {
    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        editRuleId={1}
        initialValues={editInitial}
        initialMode="advanced"
      />,
    )
    expect(screen.getByTestId('create-alert-name')).toBeDisabled()
  })

  it('submitting in edit mode calls patchMut.mutateAsync with the 5 mutable fields and NOT createMut', async () => {
    const patchMutFn = vi.fn().mockResolvedValue({})
    const createMutFn = vi.fn().mockResolvedValue({})
    vi.mocked(usePatchUserRule).mockReturnValue({
      mutateAsync: patchMutFn,
      isPending: false,
    } as never)
    vi.mocked(useCreateUserRule).mockReturnValue({
      mutateAsync: createMutFn,
      isPending: false,
    } as never)

    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        editRuleId={42}
        initialValues={editInitial}
        initialMode="advanced"
      />,
    )

    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    await waitFor(() => expect(patchMutFn).toHaveBeenCalled())

    const callArgs = patchMutFn.mock.calls[0]![0] as {
      rule_id: number
      body: Record<string, unknown>
    }
    expect(callArgs.rule_id).toBe(42)
    // Only the 5 mutable fields.
    expect(callArgs.body).toHaveProperty('expr')
    expect(callArgs.body).toHaveProperty('severity')
    expect(callArgs.body).toHaveProperty('summary')
    expect(callArgs.body).toHaveProperty('description')
    expect(callArgs.body).toHaveProperty('for_duration')
    // Immutable fields must NOT be in the PATCH body.
    expect(callArgs.body).not.toHaveProperty('rule_name')
    expect(callArgs.body).not.toHaveProperty('expr_kind')

    // Create mutation must NOT have been called.
    expect(createMutFn).not.toHaveBeenCalled()
  })

  it('create mode: title is "Create alert from query" and submit calls createMut (unchanged path)', async () => {
    const createMutFn = vi.fn().mockResolvedValue({})
    const patchMutFn = vi.fn().mockResolvedValue({})
    vi.mocked(useCreateUserRule).mockReturnValue({
      mutateAsync: createMutFn,
      isPending: false,
    } as never)
    vi.mocked(usePatchUserRule).mockReturnValue({
      mutateAsync: patchMutFn,
      isPending: false,
    } as never)

    renderWithClient(
      <CreateAlertModal
        open
        onOpenChange={vi.fn()}
        initialMode="advanced"
        initialValues={baseInitial}
      />,
    )

    expect(screen.getByText('Create alert from query')).toBeInTheDocument()
    expect(screen.getByTestId('create-alert-submit')).toHaveTextContent('Create alert')

    await act(async () => {
      fireEvent.click(screen.getByTestId('create-alert-submit'))
      await Promise.resolve()
    })
    await waitFor(() => expect(createMutFn).toHaveBeenCalled())
    expect(patchMutFn).not.toHaveBeenCalled()
  })
})
