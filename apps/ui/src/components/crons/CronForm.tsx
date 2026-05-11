import { useEffect } from 'react'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { SchedulePreviewForExpr } from '@/components/crons/SchedulePreview'
import type { components } from '@/api/schema'

type CronCreate = components['schemas']['CronCreate']
type CronUpdate = components['schemas']['CronUpdate']
type CronOut = components['schemas']['CronOut']

const integrationModes = ['observe', 'heartbeat', 'both'] as const

/**
 * Zod schema with the SAME xor validation the backend enforces. Field-level
 * cron-expression validation is debounced server-side via the SchedulePreview
 * component (which surfaces 422 messages inline); the client schema only
 * verifies length + emptiness so we do not duplicate croniter logic in JS.
 */
const cronFormSchema = z
  .object({
    name: z.string().min(1, 'Name is required').max(200),
    host: z.string().min(1, 'Host is required').max(200),
    command: z.string().min(1, 'Command is required').max(2000),
    scheduleMode: z.enum(['schedule', 'cadence']),
    schedule: z.string().max(200).optional(),
    cadence_seconds: z.number().int().min(0).max(86400),
    expected_grace_seconds: z.number().int().min(0).max(86400),
    integration_mode: z.enum(integrationModes),
    enabled: z.boolean(),
  })
  .superRefine((val, ctx) => {
    if (val.scheduleMode === 'schedule') {
      if (!val.schedule || val.schedule.trim().length === 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['schedule'],
          message: 'Schedule is required when schedule-driven',
        })
      }
      if (val.cadence_seconds > 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['cadence_seconds'],
          message: 'Cadence must be 0 when using a schedule',
        })
      }
    } else {
      if (val.cadence_seconds <= 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['cadence_seconds'],
          message: 'Cadence must be > 0 when cadence-driven',
        })
      }
    }
  })

export type CronFormValues = z.infer<typeof cronFormSchema>

export interface CronFormProps {
  mode: 'create' | 'edit'
  defaultValues?: Partial<CronOut>
  submitLabel?: string
  onSubmit: (body: CronCreate | CronUpdate) => Promise<void> | void
  onCancel?: () => void
  errorMessage?: string | null
  isSubmitting?: boolean
}

export function CronForm({
  mode,
  defaultValues,
  submitLabel,
  onSubmit,
  onCancel,
  errorMessage,
  isSubmitting,
}: CronFormProps) {
  const initial: CronFormValues = {
    name: defaultValues?.name ?? '',
    host: defaultValues?.host ?? '',
    command: defaultValues?.command ?? '',
    scheduleMode:
      defaultValues?.schedule !== null && defaultValues?.schedule !== undefined
        ? 'schedule'
        : defaultValues?.cadence_seconds !== undefined && defaultValues.cadence_seconds > 0
          ? 'cadence'
          : 'schedule',
    schedule: defaultValues?.schedule ?? '',
    cadence_seconds: defaultValues?.cadence_seconds ?? 0,
    expected_grace_seconds: defaultValues?.expected_grace_seconds ?? 300,
    integration_mode: defaultValues?.integration_mode ?? 'observe',
    enabled: defaultValues?.enabled ?? true,
  }

  const form = useForm<CronFormValues>({
    resolver: zodResolver(cronFormSchema),
    defaultValues: initial,
  })

  useEffect(() => {
    form.reset(initial)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultValues?.id])

  const scheduleMode = form.watch('scheduleMode')
  const scheduleValue = form.watch('schedule') ?? ''

  const submit = form.handleSubmit((values) => {
    const payload: CronCreate | CronUpdate = {
      name: values.name,
      host: values.host,
      command: values.command,
      schedule: values.scheduleMode === 'schedule' ? (values.schedule ?? null) : null,
      cadence_seconds: values.scheduleMode === 'cadence' ? values.cadence_seconds : 0,
      expected_grace_seconds: values.expected_grace_seconds,
      integration_mode: values.integration_mode,
      enabled: values.enabled,
    }
    void onSubmit(payload)
  })

  return (
    <form
      onSubmit={(e) => {
        submit(e).catch((err) => console.error('cron form submit error', err))
      }}
      className="space-y-4"
      noValidate
    >
      <div className="space-y-2">
        <Label htmlFor="cron-name">Name</Label>
        <Input
          id="cron-name"
          aria-invalid={form.formState.errors.name !== undefined || undefined}
          {...form.register('name')}
        />
        {form.formState.errors.name && (
          <p role="alert" className="text-sm text-red-600">
            {form.formState.errors.name.message}
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="cron-host">Host</Label>
          <Input id="cron-host" {...form.register('host')} />
          {form.formState.errors.host && (
            <p role="alert" className="text-sm text-red-600">
              {form.formState.errors.host.message}
            </p>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor="cron-mode">Integration mode</Label>
          <Select id="cron-mode" {...form.register('integration_mode')}>
            <option value="observe">observe</option>
            <option value="heartbeat">heartbeat</option>
            <option value="both">both</option>
          </Select>
        </div>
      </div>

      <div className="space-y-2">
        <Label htmlFor="cron-command">Command</Label>
        <Input id="cron-command" {...form.register('command')} />
        {form.formState.errors.command && (
          <p role="alert" className="text-sm text-red-600">
            {form.formState.errors.command.message}
          </p>
        )}
      </div>

      <fieldset className="space-y-2 rounded-md border border-border p-3">
        <legend className="px-1 text-sm font-medium">Schedule</legend>
        <div className="flex gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="radio" value="schedule" {...form.register('scheduleMode')} />
            Cron expression
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" value="cadence" {...form.register('scheduleMode')} />
            Cadence (seconds)
          </label>
        </div>

        {scheduleMode === 'schedule' ? (
          <div className="space-y-2">
            <Input
              placeholder="*/5 * * * *"
              aria-label="Cron expression"
              {...form.register('schedule')}
            />
            {form.formState.errors.schedule && (
              <p role="alert" className="text-sm text-red-600">
                {form.formState.errors.schedule.message}
              </p>
            )}
            <div className="rounded-md bg-muted/50 p-3">
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Next runs
              </p>
              <SchedulePreviewForExpr expr={scheduleValue} count={3} />
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <Input
              type="number"
              min={1}
              max={86400}
              aria-label="Cadence seconds"
              {...form.register('cadence_seconds', { valueAsNumber: true })}
            />
            {form.formState.errors.cadence_seconds && (
              <p role="alert" className="text-sm text-red-600">
                {form.formState.errors.cadence_seconds.message}
              </p>
            )}
          </div>
        )}
      </fieldset>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="cron-grace">Expected grace (seconds)</Label>
          <Input
            id="cron-grace"
            type="number"
            min={0}
            max={86400}
            {...form.register('expected_grace_seconds', { valueAsNumber: true })}
          />
        </div>
        <Controller
          control={form.control}
          name="enabled"
          render={({ field }) => (
            <label className="mt-7 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={field.value}
                onChange={(e) => field.onChange(e.target.checked)}
              />
              Enabled
            </label>
          )}
        />
      </div>

      {errorMessage !== null && errorMessage !== undefined && (
        <p
          role="alert"
          className="rounded-md border border-red-600 bg-red-600/10 p-2 text-sm text-red-600"
        >
          {errorMessage}
        </p>
      )}

      <div className="flex justify-end gap-2 pt-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Saving…' : (submitLabel ?? (mode === 'create' ? 'Create' : 'Save'))}
        </Button>
      </div>
    </form>
  )
}
