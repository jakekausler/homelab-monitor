import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { useUserRules, useCreateUserRule, usePatchUserRule } from '@/api/userRules'
import { useLogsServicesQuery } from '@/api/logs'
import { useMetricNamesQuery } from '@/api/metrics'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { LogsQlEditor } from '@/components/logs/LogsQlEditor'
import { YamlPreview } from '@/components/logs/YamlPreview'
import { buildAlertRuleYaml } from '@/components/logs/alertRuleYaml'
import { escapeLogsQlPhrase } from '@/lib/logsQlTranslate'
import { toIsoZ } from '@/lib/timeRange'
import { scaffoldLogsqlExpr } from '@/components/logs/alertExpr'

export { scaffoldLogsqlExpr }

/** Reserved LogsQL words that must not be a bare `| filter` field (mirror of the
 * backend _RESERVED_FILTER_WORDS; advisory only — backend is authoritative). */
const RESERVED_FILTER_WORDS = new Set<string>([
  'count',
  'count_uniq',
  'sum',
  'avg',
  'min',
  'max',
  'median',
  'quantile',
  'uniq',
  'uniq_values',
  'values',
  'rate',
  'histogram',
  'stats',
  'filter',
  'by',
  'sort',
  'limit',
  'offset',
  'fields',
  'format',
  'math',
  'top',
])

/**
 * Advisory (non-blocking) client warnings for an Advanced-mode logsql expr,
 * mirroring the backend heuristic validator. The backend remains authoritative
 * (returns 400 invalid_expr). PURE — exported for unit testing.
 *
 * Returns a list of human-readable warning strings (empty = no advisory issues).
 * Only meaningful for logsql; callers pass logsql exprs only.
 */
export function advancedExprWarnings(expr: string): string[] {
  const warnings: string[] = []
  const trimmed = expr.trim()
  if (trimmed.length === 0) return warnings

  // Unbalanced double-quotes (count unescaped `"`).
  let quoteCount = 0
  for (let i = 0; i < expr.length; i++) {
    if (expr[i] === '\\') {
      i++
      continue
    }
    if (expr[i] === '"') quoteCount++
  }
  if (quoteCount % 2 !== 0) {
    warnings.push('Unbalanced double-quote (") — every opening quote needs a closing quote.')
  }

  // Missing stats pipe.
  if (!/\|\s*stats\b/i.test(expr)) {
    warnings.push('Log alerts need a | stats ... | filter ... pipe to produce a numeric value.')
  }

  // Reserved word as a `| filter` field.
  const filterRe = /\|\s*filter\s+([A-Za-z_][A-Za-z0-9_]*)/gi
  let m: RegExpExecArray | null
  while ((m = filterRe.exec(expr)) !== null) {
    const field = m[1]!.toLowerCase()
    if (RESERVED_FILTER_WORDS.has(field)) {
      warnings.push(
        `"${m[1]!}" is a reserved LogsQL word; filter on the stats output alias ` +
          `(e.g. count() as match_count | filter match_count:>0).`,
      )
      break
    }
  }

  return warnings
}

/** Structured inputs for Simple mode → safe quoted LogsQL. */
export interface SimpleExprFields {
  /** Service value (empty = no service filter). */
  service: string
  /** Free-text "message contains" (may contain quotes/braces/backslashes; empty = no filter). */
  contains: string
  /** Integer N in `count > N` (>= 1). */
  threshold: number
  /** Duration window like `5m`, `30s`, `1h` (validated `^\d+[smhd]$`). */
  window: string
}

/**
 * Compile Simple-mode structured fields into a safe LogsQL alert expression.
 *
 * Mirrors the built-in vmalert-logs rule shape (deploy/vmalert/logs/system.yaml):
 *   _time:5m service:="homeassistant" "ERROR" | stats count() as match_count | filter match_count:>10
 *
 * Order (space-joined, empty parts OMITTED):
 *   1. `_time:<window>`                       — always (window emitted bare)
 *   2. `service:="<escaped>"`                 — only if service non-empty (equality + quoted value)
 *   3. `"<escaped>"`                          — only if contains non-empty (bare quoted phrase = _msg match)
 *   4. ` | stats count() as match_count | filter match_count:><threshold>`  — always
 *
 * The service value and the contains phrase are escaped via escapeLogsQlPhrase so
 * arbitrary characters (quotes, braces, backslashes) cannot break the expr.
 * PURE — no side effects. Exported for unit testing.
 */
export function buildSimpleExpr(fields: SimpleExprFields): string {
  const parts: string[] = [`_time:${fields.window}`]

  const service = fields.service.trim()
  if (service.length > 0) {
    parts.push(`service:="${escapeLogsQlPhrase(service)}"`)
  }

  // NOTE: do NOT trim `contains` — leading/trailing spaces may be meaningful in a
  // substring match. Only the empty string suppresses the filter.
  if (fields.contains.length > 0) {
    parts.push(`"${escapeLogsQlPhrase(fields.contains)}"`)
  }

  const prefix = parts.join(' ')
  return `${prefix} | stats count() as match_count | filter match_count:>${String(fields.threshold)}`
}

/** Structured inputs for metricsql Simple mode → a `<metric> <op> <threshold>` expr. */
export interface SimpleMetricsQlFields {
  /** Metric name or simple selector (validated against METRIC_NAME_REGEX). */
  metric: string
  /** Comparison operator from the fixed allow-list. */
  op: MetricsQlOperator
  /** Numeric threshold (coerced; NaN → empty expr). */
  threshold: number
}

/**
 * Compile metricsql Simple-mode fields into `<metric> <op> <threshold>`.
 *
 * SAFETY: the metric name is validated against METRIC_NAME_REGEX and the
 * operator against the fixed allow-list; on ANY invalid input (bad metric name,
 * non-finite threshold) returns '' so the empty-expr surfaces zod's
 * "Expression is required" rather than emitting a malformed/injectable expr.
 * PURE — no side effects. Exported for unit testing (mirrors buildSimpleExpr).
 */
export function buildSimpleMetricsQLExpr(fields: SimpleMetricsQlFields): string {
  const metric = fields.metric.trim()
  if (!METRIC_NAME_REGEX.test(metric)) return ''
  if (!METRICSQL_OPERATORS.includes(fields.op)) return ''
  if (!Number.isFinite(fields.threshold)) return ''
  return `${metric} ${fields.op} ${String(fields.threshold)}`
}

const RULE_NAME_REGEX = /^[a-zA-Z_][a-zA-Z0-9_]*$/
const FOR_DURATION_REGEX = /^(\d+[smhd])+$|^0s$/
const WINDOW_REGEX = /^\d+[smhd]$/
const DEFAULT_THRESHOLD = 10
const DEFAULT_WINDOW = '5m'
/** Valid PromQL/MetricsQL metric (or label-free selector) name. */
const METRIC_NAME_REGEX = /^[a-zA-Z_:][a-zA-Z0-9_:]*$/
/** Allowed metricsql Simple-mode comparison operators (fixed allow-list). */
const METRICSQL_OPERATORS = ['>', '>=', '<', '<=', '==', '!='] as const
type MetricsQlOperator = (typeof METRICSQL_OPERATORS)[number]
const DEFAULT_METRIC_OP: MetricsQlOperator = '>'
const DEFAULT_METRIC_THRESHOLD = 0
const SERVICES_LOOKBACK_MS = 24 * 60 * 60 * 1000 // 24h default range for the service dropdown
const LOGSQL_DOC_URL = 'https://docs.victoriametrics.com/victorialogs/logsql/'

const formSchema = z.object({
  rule_name: z
    .string()
    .min(1, 'Rule name is required')
    .max(200, 'Rule name is too long')
    .regex(RULE_NAME_REGEX, 'Letters, digits, underscore; must not start with a digit'),
  expr: z.string().min(1, 'Expression is required'),
  expr_kind: z.enum(['logsql', 'metricsql']),
  severity: z.enum(['info', 'warning', 'error', 'critical']),
  for_duration: z
    .string()
    .regex(FOR_DURATION_REGEX, 'Use durations like 5m, 30s, 1h, 2h30m, or 0s'),
  summary: z.string().min(1, 'Summary is required').max(1000, 'Summary is too long'),
  description: z.string().max(4000, 'Description is too long'),
  // --- Simple-mode structured sub-fields (compile source; not POSTed directly) ---
  simple_service: z.string(),
  simple_contains: z.string(),
  simple_threshold: z.number().int().min(1, 'Threshold must be at least 1'),
  simple_window: z.string().regex(WINDOW_REGEX, 'Use a duration like 5m, 30s, 1h, 2d'),
  // --- metricsql Simple-mode sub-fields (compile source; not POSTed directly) ---
  simple_metric: z.string(),
  simple_metric_op: z.enum(['>', '>=', '<', '<=', '==', '!=']),
  simple_metric_threshold: z.number(),
})

export type CreateAlertFormValues = z.infer<typeof formSchema>

const DEFAULT_VALUES: CreateAlertFormValues = {
  rule_name: '',
  expr: '',
  expr_kind: 'logsql',
  severity: 'warning',
  for_duration: '5m',
  summary: 'Alert rule',
  description: '',
  simple_service: '',
  simple_contains: '',
  simple_threshold: DEFAULT_THRESHOLD,
  simple_window: DEFAULT_WINDOW,
  simple_metric: '',
  simple_metric_op: DEFAULT_METRIC_OP,
  simple_metric_threshold: DEFAULT_METRIC_THRESHOLD,
}

export interface CreateAlertModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /**
   * Seeds rhf defaultValues (merged over DEFAULT_VALUES). May include `expr`
   * (Advanced launch) AND/OR the simple_* structured fields (Simple launch).
   */
  initialValues?: Partial<CreateAlertFormValues>
  /** Initial mode. Default 'simple'. STAGE-044 passes 'advanced' + initialValues.expr. */
  initialMode?: 'simple' | 'advanced'
  /** Provenance (FUTURE use / 044). Default 'manual'. NOT sent in POST. */
  sourceKind?: string
  /** Provenance ref (FUTURE use / 044). NOT sent in POST. */
  sourceRef?: string | null
  /** When set, the modal is in EDIT mode: patches this rule instead of creating.
   *  rule_name + expr_kind become read-only (immutable on PATCH). */
  editRuleId?: number
}

export function CreateAlertModal({
  open,
  onOpenChange,
  initialValues,
  initialMode = 'simple',
  sourceKind = 'manual',
  sourceRef = null,
  editRuleId,
}: CreateAlertModalProps): JSX.Element {
  void sourceKind
  void sourceRef

  const rulesQuery = useUserRules()
  const createMut = useCreateUserRule()
  const patchMut = usePatchUserRule()
  const [nameError, setNameError] = useState<string | null>(null)
  const [exprError, setExprError] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)

  // Active editing mode. Simple = structured fields compile into expr; Advanced =
  // raw expr editing. Re-seeded on open from initialMode.
  const [mode, setMode] = useState<'simple' | 'advanced'>(initialMode)
  // Tracks whether the user has hand-edited the expr in Advanced mode since the
  // last compile/seed. Gates the Advanced→Simple "discard manual edits" confirm.
  const [exprDirty, setExprDirty] = useState<boolean>(false)

  const mergedDefaults = useMemo<CreateAlertFormValues>(
    () => ({ ...DEFAULT_VALUES, ...initialValues }),
    [initialValues],
  )

  const form = useForm<CreateAlertFormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: mergedDefaults,
  })

  // Re-seed the form whenever the modal opens (so a fresh launch with new
  // initialValues replaces stale state). Mirrors AddProbeModal's reset-on-open.
  useEffect(() => {
    if (open) {
      form.reset(mergedDefaults)

      setNameError(null)

      setExprError(null)

      setFormError(null)

      setMode(initialMode)

      setExprDirty(false)
    }
    // form is stable from useForm; depend on open + mergedDefaults + initialMode.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mergedDefaults, initialMode])

  // --- Simple mode: recompile expr from structured fields on every change ---
  const exprKind = form.watch('expr_kind')
  const simpleService = form.watch('simple_service')
  const simpleContains = form.watch('simple_contains')
  const simpleThreshold = form.watch('simple_threshold')
  const simpleWindow = form.watch('simple_window')
  const simpleMetric = form.watch('simple_metric')
  const simpleMetricOp = form.watch('simple_metric_op')
  const simpleMetricThreshold = form.watch('simple_metric_threshold')
  useEffect(() => {
    if (mode !== 'simple') return
    let expr: string
    if (exprKind === 'metricsql') {
      // buildSimpleMetricsQLExpr returns '' on an invalid metric name / NaN
      // threshold; the empty expr surfaces zod's "Expression is required".
      const thresholdNum = Number(simpleMetricThreshold)
      expr = buildSimpleMetricsQLExpr({
        metric: simpleMetric,
        op: simpleMetricOp,
        threshold: thresholdNum,
      })
    } else {
      // Threshold/window may be transiently invalid mid-typing; guard so we never
      // build a malformed expr. Fall back to defaults for the compile only.
      const thresholdNum = Number(simpleThreshold)
      const safeThreshold =
        Number.isFinite(thresholdNum) && thresholdNum >= 1
          ? Math.floor(thresholdNum)
          : DEFAULT_THRESHOLD
      const safeWindow = WINDOW_REGEX.test(String(simpleWindow))
        ? String(simpleWindow)
        : DEFAULT_WINDOW
      expr = buildSimpleExpr({
        service: simpleService,
        contains: simpleContains,
        threshold: safeThreshold,
        window: safeWindow,
      })
    }

    form.setValue('expr', expr, { shouldValidate: true })
    // form stable; depend on expr_kind + all simple fields + mode.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    mode,
    exprKind,
    simpleService,
    simpleContains,
    simpleThreshold,
    simpleWindow,
    simpleMetric,
    simpleMetricOp,
    simpleMetricThreshold,
  ])

  // --- Services dropdown: last-24h window, computed once per open ---
  const servicesRange = useMemo(() => {
    const now = new Date()
    const start = new Date(now.getTime() - SERVICES_LOOKBACK_MS)
    return { start: toIsoZ(start), end: toIsoZ(now) }
    // Recompute only when the modal (re)opens so the query key stays stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  const servicesQuery = useLogsServicesQuery(servicesRange.start, servicesRange.end)
  const serviceOptions = useMemo(() => servicesQuery.data?.services ?? [], [servicesQuery.data])

  // --- Metric-name autocomplete (MetricsQL Simple mode): VM __name__ discovery ---
  const metricNamesQuery = useMetricNamesQuery()
  const metricNameOptions = useMemo(
    () => metricNamesQuery.data?.names ?? [],
    [metricNamesQuery.data],
  )

  // --- Live YAML preview (debounced ~300ms) ---
  const watched = form.watch()
  const [previewYaml, setPreviewYaml] = useState<string>('')
  useEffect(() => {
    const handle = setTimeout(() => {
      setPreviewYaml(
        buildAlertRuleYaml({
          rule_name: watched.rule_name || '<rule_name>',
          expr: watched.expr || '',
          expr_kind: watched.expr_kind,
          severity: watched.severity,
          summary: watched.summary || '',
          description: watched.description || '',
          for_duration: watched.for_duration || '0s',
        }),
      )
    }, 300)
    return () => clearTimeout(handle)
  }, [
    watched.rule_name,
    watched.expr,
    watched.expr_kind,
    watched.severity,
    watched.summary,
    watched.description,
    watched.for_duration,
  ])

  // --- Client-side name-uniqueness (debounced), D3 ---
  const existingNames = useMemo<Set<string>>(
    () =>
      new Set(
        (rulesQuery.data?.rules ?? []).filter((r) => r.id !== editRuleId).map((r) => r.rule_name),
      ),
    [rulesQuery.data, editRuleId],
  )
  const watchedName = form.watch('rule_name')
  // Holds the pending uniqueness-debounce timer so the submit path can cancel it.
  // Without this, a debounce timer scheduled BEFORE submit (carrying pre-submit
  // state, e.g. nameError=null) can fire AFTER the submit catch sets the
  // server-authoritative 409 conflict error and silently clobber it back to null.
  // This race is timing-dependent and only surfaces under load (e.g. `make verify`
  // running backend pytest + vitest concurrently), where the catch lands inside
  // the 300ms debounce window. See STAGE-004-043A debugging.
  const uniqDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const handle = setTimeout(() => {
      setNameError(
        watchedName.length > 0 && existingNames.has(watchedName)
          ? 'A rule with that name already exists'
          : null,
      )
    }, 300)
    uniqDebounceRef.current = handle
    return () => {
      clearTimeout(handle)
      if (uniqDebounceRef.current === handle) uniqDebounceRef.current = null
    }
  }, [watchedName, existingNames])

  const handleOpenChange = (next: boolean): void => {
    onOpenChange(next)
  }

  // Simple → Advanced: freeze compiled expr for hand-editing; start NOT dirty.
  const switchToAdvanced = (): void => {
    setMode('advanced')
    setExprDirty(false)
  }

  // Advanced → Simple: if the user hand-edited the expr, confirm before discarding.
  // On confirm (or if not dirty), recompile from the structured fields.
  const switchToSimple = (): void => {
    if (exprDirty) {
      const ok = window.confirm(
        'Switching to Simple will rebuild the expression from the form fields and discard your manual edits. Continue?',
      )
      if (!ok) return
    }
    setMode('simple')
    setExprDirty(false)
    // Recompile immediately so the expr reflects the structured fields (the
    // recompile effect also fires, but do it here so the value is correct even
    // before the effect runs).
    let recompiled: string
    if (form.getValues('expr_kind') === 'metricsql') {
      recompiled = buildSimpleMetricsQLExpr({
        metric: form.getValues('simple_metric'),
        op: form.getValues('simple_metric_op'),
        threshold: Number(form.getValues('simple_metric_threshold')),
      })
    } else {
      const thresholdNum = Number(form.getValues('simple_threshold'))
      const safeThreshold =
        Number.isFinite(thresholdNum) && thresholdNum >= 1
          ? Math.floor(thresholdNum)
          : DEFAULT_THRESHOLD
      const win = form.getValues('simple_window')
      const safeWindow = WINDOW_REGEX.test(String(win)) ? String(win) : DEFAULT_WINDOW
      recompiled = buildSimpleExpr({
        service: form.getValues('simple_service'),
        contains: form.getValues('simple_contains'),
        threshold: safeThreshold,
        window: safeWindow,
      })
    }
    form.setValue('expr', recompiled, { shouldValidate: true })
  }

  // Set an authoritative name error from the submit path, cancelling any pending
  // uniqueness-debounce timer so a stale timer cannot clobber it back to null.
  const setAuthoritativeNameError = (msg: string): void => {
    if (uniqDebounceRef.current !== null) {
      clearTimeout(uniqDebounceRef.current)
      uniqDebounceRef.current = null
    }
    setNameError(msg)
  }

  const submit = form.handleSubmit(async (vals: CreateAlertFormValues) => {
    // Client-side uniqueness backstop before POST (create only; name immutable on edit).
    if (editRuleId === undefined && existingNames.has(vals.rule_name)) {
      setAuthoritativeNameError('A rule with that name already exists')
      return
    }
    try {
      if (editRuleId !== undefined) {
        await patchMut.mutateAsync({
          rule_id: editRuleId,
          body: {
            expr: vals.expr,
            severity: vals.severity,
            summary: vals.summary,
            description: vals.description,
            for_duration: vals.for_duration,
          },
        })
        toast.success(`Saved alert rule ${vals.rule_name}`)
        onOpenChange(false)
        return
      }
      await createMut.mutateAsync({
        rule_name: vals.rule_name,
        expr: vals.expr,
        expr_kind: vals.expr_kind,
        severity: vals.severity,
        summary: vals.summary,
        description: vals.description,
        for_duration: vals.for_duration,
        // NOTE: source_kind/source_ref are NOT in LogUserRuleCreateRequest (042) — omit.
      })
      toast.success(`Created alert rule ${vals.rule_name}`)
      onOpenChange(false)
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          // Authoritative backstop: same inline name error, keep modal open.
          setAuthoritativeNameError('A rule with that name already exists')
        } else if (err.status === 400 && err.code === 'invalid_expr') {
          // Surface the backend reason inline on the expr field; keep modal open.
          setExprError(err.message || 'Invalid expression')
        } else if (err.status === 400) {
          // Field validation error (e.g. invalid_rule: bad duration, bad severity, etc.)
          // Surface inline as a form error; keep modal open.
          setFormError(err.message || 'Invalid rule')
        } else {
          toast.error(err.message || 'Failed to save alert rule')
        }
      } else {
        toast.error('Failed to save alert rule')
      }
    }
  })

  const exprValue = form.watch('expr')
  // Advisory (non-blocking) warnings for Advanced logsql expr — mirror backend.
  const exprWarnings =
    mode === 'advanced' && form.watch('expr_kind') === 'logsql'
      ? advancedExprWarnings(exprValue)
      : []

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-3xl"
        data-testid="create-alert-modal"
      >
        <DialogHeader>
          <DialogTitle>
            {editRuleId !== undefined
              ? 'Edit alert rule'
              : sourceKind !== undefined
                ? 'Create alert from query'
                : 'Create alert rule'}
          </DialogTitle>
          <DialogDescription>
            Define a vmalert rule. The YAML preview is rendered client-side; the actual rule is
            rendered server-side on save.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            submit(e).catch((err) => console.error('create-alert submit error', err))
          }}
          noValidate
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {/* ---------- LEFT: form ---------- */}
            <div className="space-y-4">
              {/* rule_name */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-name">Rule name *</Label>
                <Input
                  id="create-alert-name"
                  data-testid="create-alert-name"
                  placeholder="MyAlertRule"
                  disabled={editRuleId !== undefined}
                  {...form.register('rule_name')}
                />
                {form.formState.errors.rule_name && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.rule_name.message}
                  </p>
                )}
                {nameError && (
                  <p
                    role="alert"
                    data-testid="create-alert-name-conflict"
                    className="text-sm text-red-600"
                  >
                    {nameError}
                  </p>
                )}
              </div>

              {/* expr_kind */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-expr-kind">Rule type</Label>
                <Select
                  id="create-alert-expr-kind"
                  data-testid="create-alert-expr-kind"
                  disabled={editRuleId !== undefined}
                  {...form.register('expr_kind')}
                >
                  <option value="logsql">Logs (LogsQL)</option>
                  <option value="metricsql">Metrics (MetricsQL)</option>
                </Select>
              </div>

              {/* severity */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-severity">Severity</Label>
                <Select
                  id="create-alert-severity"
                  data-testid="create-alert-severity"
                  {...form.register('severity')}
                >
                  <option value="info">info</option>
                  <option value="warning">warning</option>
                  <option value="error">error</option>
                  <option value="critical">critical</option>
                </Select>
              </div>

              {/* ---------- mode toggle (segmented control) ---------- */}
              <div className="space-y-1">
                <Label>Mode</Label>
                <div
                  role="tablist"
                  aria-label="Alert builder mode"
                  className="inline-flex rounded-md border border-input p-0.5"
                  data-testid="create-alert-mode-toggle"
                >
                  <Button
                    type="button"
                    size="sm"
                    variant={mode === 'simple' ? 'default' : 'ghost'}
                    role="tab"
                    aria-selected={mode === 'simple'}
                    data-testid="create-alert-mode-simple"
                    onClick={switchToSimple}
                  >
                    Simple
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant={mode === 'advanced' ? 'default' : 'ghost'}
                    role="tab"
                    aria-selected={mode === 'advanced'}
                    data-testid="create-alert-mode-advanced"
                    onClick={switchToAdvanced}
                  >
                    Advanced
                  </Button>
                </div>
              </div>

              {/* ---------- SIMPLE mode fields (logsql) ---------- */}
              {mode === 'simple' && form.watch('expr_kind') === 'logsql' && (
                <div className="space-y-4" data-testid="create-alert-simple-fields">
                  {/* service */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-service">Service</Label>
                    <Select
                      id="create-alert-service"
                      data-testid="create-alert-service"
                      {...form.register('simple_service')}
                    >
                      <option value="">Any service</option>
                      {serviceOptions.map((s) => (
                        <option key={`${s.source_type}:${s.service}`} value={s.service}>
                          {s.service} ({s.source_type}, {s.count})
                        </option>
                      ))}
                    </Select>
                  </div>

                  {/* contains */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-contains">Message contains</Label>
                    <Input
                      id="create-alert-contains"
                      data-testid="create-alert-contains"
                      placeholder="e.g. Out of memory"
                      {...form.register('simple_contains')}
                    />
                  </div>

                  {/* threshold */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-threshold">Alert when count exceeds</Label>
                    <Input
                      id="create-alert-threshold"
                      data-testid="create-alert-threshold"
                      type="number"
                      min={1}
                      {...form.register('simple_threshold', { valueAsNumber: true })}
                    />
                    {form.formState.errors.simple_threshold && (
                      <p role="alert" className="text-sm text-red-600">
                        {form.formState.errors.simple_threshold.message}
                      </p>
                    )}
                  </div>

                  {/* window */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-window">Over time window</Label>
                    <Input
                      id="create-alert-window"
                      data-testid="create-alert-window"
                      placeholder="5m"
                      {...form.register('simple_window')}
                    />
                    {form.formState.errors.simple_window && (
                      <p role="alert" className="text-sm text-red-600">
                        {form.formState.errors.simple_window.message}
                      </p>
                    )}
                  </div>
                </div>
              )}

              {/* ---------- SIMPLE mode fields (metricsql) ---------- */}
              {mode === 'simple' && form.watch('expr_kind') === 'metricsql' && (
                <div className="space-y-4" data-testid="create-alert-simple-metrics-fields">
                  {/* metric — native datalist autocomplete over VM metric names.
                      Type-to-filter the real metric list, but a custom name (a
                      metric that does not exist yet) is still allowed. */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-metric">Metric</Label>
                    <Input
                      id="create-alert-metric"
                      data-testid="create-alert-metric"
                      list="create-alert-metric-options"
                      placeholder="e.g. up or node_filesystem_avail_bytes"
                      autoComplete="off"
                      {...form.register('simple_metric')}
                    />
                    <datalist
                      id="create-alert-metric-options"
                      data-testid="create-alert-metric-datalist"
                    >
                      {metricNameOptions.map((name) => (
                        <option key={name} value={name} />
                      ))}
                    </datalist>
                  </div>

                  {/* operator */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-metric-op">Comparison</Label>
                    <Select
                      id="create-alert-metric-op"
                      data-testid="create-alert-metric-op"
                      {...form.register('simple_metric_op')}
                    >
                      <option value=">">&gt;</option>
                      <option value=">=">&gt;=</option>
                      <option value="<">&lt;</option>
                      <option value="<=">&lt;=</option>
                      <option value="==">==</option>
                      <option value="!=">!=</option>
                    </Select>
                  </div>

                  {/* threshold */}
                  <div className="space-y-1">
                    <Label htmlFor="create-alert-metric-threshold">Threshold</Label>
                    <Input
                      id="create-alert-metric-threshold"
                      data-testid="create-alert-metric-threshold"
                      type="number"
                      step="any"
                      {...form.register('simple_metric_threshold', { valueAsNumber: true })}
                    />
                  </div>
                </div>
              )}

              {/* ---------- ADVANCED mode: LogsQL expr editor ---------- */}
              {mode === 'advanced' && form.watch('expr_kind') === 'logsql' && (
                <div className="space-y-1" data-testid="create-alert-advanced-fields">
                  <Label htmlFor="create-alert-expr">Expression *</Label>
                  <LogsQlEditor
                    value={exprValue}
                    onChange={(next) => {
                      form.setValue('expr', next, { shouldValidate: true })
                      setExprDirty(true)
                      setExprError(null)
                      setFormError(null)
                    }}
                    onSubmit={() => {
                      /* Enter in the expr editor must NOT submit the modal form. */
                    }}
                    ariaLabel="Alert expression"
                  />
                  <p className="text-xs text-muted-foreground">
                    Uses LogsQL —{' '}
                    <a
                      href={LOGSQL_DOC_URL}
                      target="_blank"
                      rel="noreferrer"
                      className="underline"
                      data-testid="create-alert-logsql-doc-link"
                    >
                      see reference ↗
                    </a>
                  </p>
                  {exprWarnings.length > 0 && (
                    <ul
                      role="status"
                      data-testid="create-alert-expr-warnings"
                      className="space-y-1 text-sm text-amber-600"
                    >
                      {exprWarnings.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  )}
                  {exprError && (
                    <p
                      role="alert"
                      data-testid="create-alert-expr-error"
                      className="text-sm text-red-600"
                    >
                      {exprError}
                    </p>
                  )}
                  {form.formState.errors.expr && (
                    <p role="alert" className="text-sm text-red-600">
                      {form.formState.errors.expr.message}
                    </p>
                  )}
                </div>
              )}

              {/* ---------- ADVANCED mode: MetricsQL expr editor ---------- */}
              {mode === 'advanced' && form.watch('expr_kind') === 'metricsql' && (
                <div className="space-y-1" data-testid="create-alert-advanced-fields">
                  <Label htmlFor="create-alert-expr">Expression *</Label>
                  <textarea
                    id="create-alert-expr"
                    data-testid="create-alert-expr"
                    rows={3}
                    className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    value={exprValue}
                    onChange={(e) => {
                      form.setValue('expr', e.target.value, { shouldValidate: true })
                      setExprDirty(true)
                      setExprError(null)
                      setFormError(null)
                    }}
                    aria-label="Alert expression"
                  />
                  <p className="text-xs text-muted-foreground">Uses MetricsQL</p>
                  {exprError && (
                    <p
                      role="alert"
                      data-testid="create-alert-expr-error"
                      className="text-sm text-red-600"
                    >
                      {exprError}
                    </p>
                  )}
                  {form.formState.errors.expr && (
                    <p role="alert" className="text-sm text-red-600">
                      {form.formState.errors.expr.message}
                    </p>
                  )}
                </div>
              )}

              {/* for_duration */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-for">For duration</Label>
                <Input
                  id="create-alert-for"
                  data-testid="create-alert-for"
                  placeholder="5m"
                  {...form.register('for_duration', {
                    onChange: () => {
                      setFormError(null)
                    },
                  })}
                />
                {form.formState.errors.for_duration && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.for_duration.message}
                  </p>
                )}
              </div>

              {/* summary */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-summary">Summary *</Label>
                <Input
                  id="create-alert-summary"
                  data-testid="create-alert-summary"
                  {...form.register('summary')}
                />
                {form.formState.errors.summary && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.summary.message}
                  </p>
                )}
              </div>

              {/* description */}
              <div className="space-y-1">
                <Label htmlFor="create-alert-description">Description</Label>
                <textarea
                  id="create-alert-description"
                  data-testid="create-alert-description"
                  rows={3}
                  className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  {...form.register('description')}
                />
                {form.formState.errors.description && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.description.message}
                  </p>
                )}
              </div>
            </div>

            {/* ---------- RIGHT: YAML preview ---------- */}
            <div className="space-y-1">
              <Label>Preview</Label>
              <YamlPreview value={previewYaml} ariaLabel="Rule YAML preview" />
              <p className="text-xs text-muted-foreground">
                Client-side preview. The actual rule is rendered server-side on save.
              </p>
            </div>
          </div>

          <DialogFooter className="gap-2 pt-4 sm:gap-0">
            {formError && (
              <div className="w-full">
                <p
                  role="alert"
                  data-testid="create-alert-form-error"
                  className="mb-3 text-sm text-red-600"
                >
                  {formError}
                </p>
              </div>
            )}
            <div className="flex gap-2 sm:justify-end">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="create-alert-submit"
                disabled={createMut.isPending || patchMut.isPending}
              >
                {editRuleId !== undefined
                  ? patchMut.isPending
                    ? 'Saving…'
                    : 'Save changes'
                  : createMut.isPending
                    ? 'Saving…'
                    : 'Create alert'}
              </Button>
            </div>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
