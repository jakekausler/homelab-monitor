import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import type { JSX } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { EmptyState } from '@/components/EmptyState'
import {
  useSilenceAllowlist,
  useCreateSilenceAllowlistEntry,
  useDeleteSilenceAllowlistEntry,
  type SilenceAllowlistCreateRequest,
} from '@/api/silenceAllowlist'
import { useSignaturesQuery } from '@/api/signatures'
import type { SignatureResponse } from '@/api/signatures'
import { formatRelative } from '@/lib/relativeTime'

const formSchema = z
  .object({
    service_key: z.string().min(1, 'Service key is required').max(200),
    template_hash: z.string().max(200),
    schedule_kind: z.enum(['always', 'cron', 'window']),
    schedule_value: z.string().max(200),
    window_start: z.string(),
    window_end: z.string(),
    reason: z.string().min(1, 'Reason is required').max(2000),
    expires_at: z.string(),
  })
  .superRefine((vals, ctx) => {
    if (vals.schedule_kind === 'cron' && vals.schedule_value.trim().length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Cron expression is required',
        path: ['schedule_value'],
      })
    }
    if (vals.schedule_kind === 'window') {
      if (vals.window_start.length === 0 || vals.window_end.length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Both start and end are required for a window',
          path: ['window_start'],
        })
      } else if (new Date(vals.window_start) > new Date(vals.window_end)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Start must be on or before end',
          path: ['window_start'],
        })
      }
    }
  })

type FormValues = z.infer<typeof formSchema>

export function SilenceAllowlistTab(): JSX.Element {
  const { data, isLoading, error } = useSilenceAllowlist()
  const create = useCreateSilenceAllowlistEntry()
  const del = useDeleteSilenceAllowlistEntry()
  const [submitError, setSubmitError] = useState<string | null>(null)

  const signaturesQuery = useSignaturesQuery({ limit: 500 })
  const allSignatures: SignatureResponse[] = signaturesQuery.data?.signatures ?? []

  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      service_key: '',
      template_hash: '',
      schedule_kind: 'always',
      schedule_value: '',
      window_start: '',
      window_end: '',
      reason: '',
      expires_at: '',
    },
  })

  const scheduleKind = form.watch('schedule_kind')

  function truncateTemplate(s: string, max: number): string {
    return s.length > max ? s.slice(0, max) + '…' : s
  }

  function handlePickerChange(e: React.ChangeEvent<HTMLSelectElement>): void {
    const val = e.target.value
    if (!val) return
    const sig = allSignatures.find((s) => `${s.service_key}:${s.template_hash}` === val)
    if (!sig) return
    form.setValue('service_key', sig.service_key, { shouldValidate: true })
    form.setValue('template_hash', sig.template_hash, { shouldValidate: true })
  }

  const submit = form.handleSubmit((vals) => {
    setSubmitError(null)

    const body: SilenceAllowlistCreateRequest = {
      service_key: vals.service_key,
      schedule_kind: vals.schedule_kind,
      schedule_value:
        vals.schedule_kind === 'window'
          ? `${new Date(vals.window_start).toISOString()}/${new Date(vals.window_end).toISOString()}`
          : vals.schedule_kind === 'cron'
            ? vals.schedule_value
            : '',
      reason: vals.reason,
      ...(vals.template_hash.length > 0 ? { template_hash: vals.template_hash } : {}),
      ...(vals.expires_at.length > 0
        ? { expires_at: new Date(vals.expires_at).toISOString() }
        : {}),
    }

    create.mutate(body, {
      onSuccess: () => {
        form.reset()
        setSubmitError(null)
      },
      onError: (err) => {
        setSubmitError(err.message || 'Failed to create entry')
      },
    })
  })

  if (error !== null) {
    return <div className="p-4 text-sm text-destructive">Error loading silence allowlist</div>
  }

  const entries = data?.entries ?? []

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        {/* Description block */}
        <div data-testid="silence-allowlist-description">
          <h1 className="text-2xl font-semibold tracking-tight">Silence Allowlist</h1>
          <p className="text-sm text-muted-foreground">
            {
              'Mark recurring log signatures whose silence is expected. Matching signatures won\'t trigger a "signature went silent" alert when they stop appearing. This does not affect log storage or any other alerts.'
            }
          </p>
        </div>

        {/* Form Section */}
        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-4 text-lg font-semibold">Create Entry</h2>
          <form
            onSubmit={(e) => {
              submit(e).catch((err) => console.error('form submit error', err))
            }}
            className="space-y-4"
            noValidate
          >
            {/* Signature picker */}
            <div>
              <label htmlFor="signature-picker" className="mb-1 block text-sm font-medium">
                Pick a signature (optional)
              </label>
              {signaturesQuery.isLoading && (
                <p className="text-sm text-muted-foreground">Loading signatures…</p>
              )}
              {!signaturesQuery.isLoading && (
                <select
                  id="signature-picker"
                  data-testid="silence-signature-picker"
                  onChange={handlePickerChange}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                  defaultValue=""
                >
                  <option value="">— Select a signature to autofill —</option>
                  {allSignatures.map((sig) => (
                    <option
                      key={`${sig.service_key}:${sig.template_hash}`}
                      value={`${sig.service_key}:${sig.template_hash}`}
                    >
                      {sig.service_key} — {truncateTemplate(sig.template_str, 60)}
                    </option>
                  ))}
                </select>
              )}
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="service-key">Service Key *</Label>
                <Input
                  id="service-key"
                  placeholder="e.g., plex, homeassistant"
                  data-testid="silence-service-key"
                  {...form.register('service_key')}
                />
                {form.formState.errors.service_key && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.service_key.message}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="template-hash">Signature (optional)</Label>
                <Input
                  id="template-hash"
                  placeholder="Leave empty for all signatures"
                  data-testid="silence-template-hash"
                  {...form.register('template_hash')}
                />
                {form.formState.errors.template_hash && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.template_hash.message}
                  </p>
                )}
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="schedule-kind">Schedule Type *</Label>
              <select
                id="schedule-kind"
                data-testid="silence-schedule-kind"
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                {...form.register('schedule_kind')}
              >
                <option value="always">Always</option>
                <option value="cron">Cron</option>
                <option value="window">Window</option>
              </select>
            </div>

            {scheduleKind === 'cron' && (
              <div className="space-y-2">
                <Label htmlFor="schedule-value">Cron Expression *</Label>
                <Input
                  id="schedule-value"
                  placeholder="0 * * * *"
                  data-testid="silence-cron-value"
                  {...form.register('schedule_value')}
                />
                {form.formState.errors.schedule_value && (
                  <p role="alert" className="text-sm text-red-600">
                    {form.formState.errors.schedule_value.message}
                  </p>
                )}
              </div>
            )}

            {scheduleKind === 'window' && (
              <div className="space-y-2">
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="window-start">Start Time *</Label>
                    <Input
                      id="window-start"
                      type="datetime-local"
                      data-testid="silence-window-start"
                      {...form.register('window_start')}
                    />
                    {form.formState.errors.window_start && (
                      <p role="alert" className="text-sm text-red-600">
                        {form.formState.errors.window_start.message}
                      </p>
                    )}
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="window-end">End Time *</Label>
                    <Input
                      id="window-end"
                      type="datetime-local"
                      data-testid="silence-window-end"
                      {...form.register('window_end')}
                    />
                    {form.formState.errors.window_end && (
                      <p role="alert" className="text-sm text-red-600">
                        {form.formState.errors.window_end.message}
                      </p>
                    )}
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  Times are interpreted in your local timezone.
                </p>
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="reason">Reason *</Label>
              <Input
                id="reason"
                placeholder="Why is this silence expected?"
                data-testid="silence-reason"
                {...form.register('reason')}
              />
              {form.formState.errors.reason && (
                <p role="alert" className="text-sm text-red-600">
                  {form.formState.errors.reason.message}
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="expires-at">Expires At (optional)</Label>
              <Input
                id="expires-at"
                type="datetime-local"
                placeholder="Leave empty for no expiration"
                data-testid="silence-expires-at"
                {...form.register('expires_at')}
              />
              {form.formState.errors.expires_at && (
                <p role="alert" className="text-sm text-red-600">
                  {form.formState.errors.expires_at.message}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                Time is interpreted in your local timezone.
              </p>
            </div>

            {submitError && (
              <p
                role="alert"
                className="rounded-md border border-red-600 bg-red-600/10 p-2 text-sm text-red-600"
              >
                {submitError}
              </p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <Button type="submit" disabled={create.isPending}>
                {create.isPending ? 'Creating…' : 'Create Entry'}
              </Button>
            </div>
          </form>
        </div>

        {/* List Section */}
        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-4 text-lg font-semibold">Entries</h2>

          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

          {entries.length === 0 && !isLoading && (
            <EmptyState>No silence allowlist entries yet.</EmptyState>
          )}

          {entries.length > 0 && (
            <>
              {/* Desktop Table */}
              <div className="hidden md:block overflow-x-auto">
                <table
                  data-testid="silence-allowlist-table"
                  className="w-full text-sm border-collapse"
                >
                  <thead>
                    <tr className="border-b border-border">
                      <th className="px-4 py-2 text-left font-medium">Service</th>
                      <th className="px-4 py-2 text-left font-medium">Signature</th>
                      <th className="px-4 py-2 text-left font-medium">Kind</th>
                      <th className="px-4 py-2 text-left font-medium">Value</th>
                      <th className="px-4 py-2 text-left font-medium">Reason</th>
                      <th className="px-4 py-2 text-left font-medium">Created</th>
                      <th className="px-4 py-2 text-left font-medium">Expires</th>
                      <th className="px-4 py-2 text-left font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((e) => (
                      <tr
                        key={e.id}
                        data-testid="silence-allowlist-row"
                        data-entry-id={e.id}
                        className="border-b border-border hover:bg-muted/50"
                      >
                        <td className="px-4 py-2">{e.service_key}</td>
                        <td className="px-4 py-2 text-xs font-mono">
                          {e.template_hash ?? '(all signatures)'}
                        </td>
                        <td className="px-4 py-2">
                          <span className="inline-block rounded bg-blue-100 px-2 py-1 text-xs font-medium text-blue-800 dark:bg-blue-900 dark:text-blue-100">
                            {e.schedule_kind}
                          </span>
                        </td>
                        <td className="px-4 py-2 max-w-xs truncate text-xs font-mono">
                          {e.schedule_kind === 'always' ? '—' : e.schedule_value}
                        </td>
                        <td className="px-4 py-2 max-w-xs truncate">{e.reason}</td>
                        <td className="px-4 py-2 text-xs">{formatRelative(e.created_at)}</td>
                        <td className="px-4 py-2 text-xs">
                          {e.expires_at ? formatRelative(e.expires_at) : '—'}
                        </td>
                        <td className="px-4 py-2">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            data-testid="silence-delete"
                            onClick={() => del.mutate(e.id)}
                            disabled={del.isPending}
                          >
                            {del.isPending ? 'Deleting…' : 'Delete'}
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Mobile Cards */}
              <ul data-testid="silence-allowlist-cards" className="md:hidden space-y-2">
                {entries.map((e) => (
                  <li
                    key={e.id}
                    data-testid="silence-allowlist-row"
                    data-entry-id={e.id}
                    className="rounded border border-border bg-muted/50 p-3 space-y-2"
                  >
                    <div className="flex items-center justify-between">
                      <div className="font-medium text-sm">{e.service_key}</div>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        data-testid="silence-delete"
                        onClick={() => del.mutate(e.id)}
                        disabled={del.isPending}
                      >
                        {del.isPending ? 'Deleting…' : 'Delete'}
                      </Button>
                    </div>
                    {e.template_hash && (
                      <div className="text-xs font-mono text-muted-foreground">
                        Hash: {e.template_hash}
                      </div>
                    )}
                    <div className="flex items-center gap-2">
                      <span className="inline-block rounded bg-blue-100 px-2 py-1 text-xs font-medium text-blue-800 dark:bg-blue-900 dark:text-blue-100">
                        {e.schedule_kind}
                      </span>
                      {e.schedule_kind !== 'always' && (
                        <span className="text-xs font-mono text-muted-foreground">
                          {e.schedule_value}
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-muted-foreground">{e.reason}</div>
                    <div className="text-xs text-muted-foreground space-y-1">
                      <div>Created: {formatRelative(e.created_at)}</div>
                      {e.expires_at && <div>Expires: {formatRelative(e.expires_at)}</div>}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
