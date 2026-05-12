import { useEffect } from 'react'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { components } from '@/api/schema'

type CronUpdate = components['schemas']['CronUpdate']
type CronOut = components['schemas']['CronOut']

const cronFormSchema = z.object({
  name: z.string().min(1, 'Name is required').max(200),
  expected_grace_seconds: z.number().int().min(0).max(86400),
  enabled: z.boolean(),
})

export type CronFormValues = z.infer<typeof cronFormSchema>

export interface CronFormProps {
  defaultValues?: Partial<CronOut>
  submitLabel?: string
  onSubmit: (body: CronUpdate) => Promise<void> | void
  onCancel?: () => void
  errorMessage?: string | null
  isSubmitting?: boolean
}

export function CronForm({
  defaultValues,
  submitLabel,
  onSubmit,
  onCancel,
  errorMessage,
  isSubmitting,
}: CronFormProps) {
  const initial: CronFormValues = {
    name: defaultValues?.name ?? '',
    expected_grace_seconds: defaultValues?.expected_grace_seconds ?? 300,
    enabled: defaultValues?.enabled ?? true,
  }

  const form = useForm<CronFormValues>({
    resolver: zodResolver(cronFormSchema),
    defaultValues: initial,
  })

  useEffect(() => {
    form.reset(initial)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultValues?.fingerprint])

  const submit = form.handleSubmit((values) => {
    const payload: CronUpdate = {
      name: values.name,
      expected_grace_seconds: values.expected_grace_seconds,
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
          {isSubmitting ? 'Saving…' : (submitLabel ?? 'Save')}
        </Button>
      </div>
    </form>
  )
}
