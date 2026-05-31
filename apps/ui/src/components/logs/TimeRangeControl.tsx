import { useState } from 'react'
import { ChevronDown } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useMediaQuery } from '@/lib/useMediaQuery'
import {
  ALL_PRESETS,
  fromDatetimeLocalValue,
  toDatetimeLocalValue,
  validatePartialRange,
  type PresetToken,
  type TimeRangeValue,
} from '@/lib/timeRange'

export interface TimeRangeControlProps {
  value: TimeRangeValue
  onChange: (v: TimeRangeValue) => void
  /** "bounded" = cron: custom range must lie within [min, max]. */
  mode?: 'full' | 'bounded'
  /** Lower bound for bounded mode (cron run window start). */
  min?: Date | undefined
  /** Upper bound for bounded mode (cron run window end). */
  max?: Date | undefined
  /** Which presets to show; default all 6. */
  presets?: readonly PresetToken[]
}

function labelForValue(value: TimeRangeValue): string {
  if (value.kind === 'preset') return `Last ${value.token}`
  const fmt = (d: Date): string => toDatetimeLocalValue(d).replace('T', ' ')
  const startLabel = value.start ? fmt(value.start) : 'Earliest'
  const endLabel = value.end ? fmt(value.end) : 'Now'
  return `${startLabel} → ${endLabel}`
}

/**
 * Controlled time-range selector. The URL is the single source of truth — the
 * VIEWER owns URL sync; this control just emits {kind:'preset'|'custom'} via
 * onChange. SCAFFOLDING: also consumed by STAGE-004-010 Explorer.
 */
export function TimeRangeControl({
  value,
  onChange,
  mode = 'full',
  min,
  max,
  presets = ALL_PRESETS,
}: TimeRangeControlProps) {
  const isDesktop = useMediaQuery('(min-width: 640px)')
  const [open, setOpen] = useState(false)
  const [showCustom, setShowCustom] = useState(value.kind === 'custom')

  // Local draft state for the two inputs (datetime-local string form).
  const initialStart =
    value.kind === 'custom' && value.start ? toDatetimeLocalValue(value.start) : ''
  const initialEnd = value.kind === 'custom' && value.end ? toDatetimeLocalValue(value.end) : ''
  const [startStr, setStartStr] = useState(initialStart)
  const [endStr, setEndStr] = useState(initialEnd)
  const [error, setError] = useState<string | null>(null)

  const handleOpenChange = (next: boolean): void => {
    if (next) {
      setShowCustom(value.kind === 'custom')
      setStartStr(value.kind === 'custom' && value.start ? toDatetimeLocalValue(value.start) : '')
      setEndStr(value.kind === 'custom' && value.end ? toDatetimeLocalValue(value.end) : '')
      setError(null)
    }
    setOpen(next)
  }

  const minAttr = min !== undefined ? toDatetimeLocalValue(min) : undefined
  const maxAttr = max !== undefined ? toDatetimeLocalValue(max) : undefined

  const selectPreset = (token: PresetToken): void => {
    onChange({ kind: 'preset', token })
    setOpen(false)
  }

  const applyCustom = (): void => {
    // Either field may be empty → fromDatetimeLocalValue returns null for empty.
    const start = fromDatetimeLocalValue(startStr)
    const end = fromDatetimeLocalValue(endStr)
    const result = validatePartialRange(start, end, {
      min: mode === 'bounded' ? min : undefined,
      max: mode === 'bounded' ? max : undefined,
    })
    if (!result.ok) {
      setError(result.error)
      return
    }
    setError(null)
    onChange({
      kind: 'custom',
      start: start ?? undefined,
      end: end ?? undefined,
    })
    setOpen(false)
  }

  const panel = (
    <div className="space-y-3 p-2" data-testid="time-range-panel">
      <div className="flex flex-wrap gap-1.5">
        {presets.map((p) => (
          <Button
            key={p}
            size="sm"
            variant={value.kind === 'preset' && value.token === p ? 'default' : 'outline'}
            data-testid={`preset-${p}`}
            onClick={() => {
              selectPreset(p)
            }}
          >
            {p}
          </Button>
        ))}
      </div>
      {!showCustom && (
        <Button
          size="sm"
          variant="outline"
          data-testid="custom-range-toggle"
          onClick={() => {
            setShowCustom(true)
          }}
        >
          Custom range…
        </Button>
      )}
      {showCustom && (
        <div className="space-y-2" data-testid="custom-range-fields">
          <div className="space-y-1">
            <Label htmlFor="trc-start">Start</Label>
            <Input
              id="trc-start"
              type="datetime-local"
              data-testid="custom-start"
              value={startStr}
              min={minAttr}
              max={maxAttr}
              onChange={(e) => {
                setStartStr(e.target.value)
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="trc-end">End</Label>
            <Input
              id="trc-end"
              type="datetime-local"
              data-testid="custom-end"
              value={endStr}
              min={minAttr}
              max={maxAttr}
              onChange={(e) => {
                setEndStr(e.target.value)
              }}
            />
          </div>
          {error !== null && (
            <p role="alert" className="text-sm text-red-600" data-testid="custom-range-error">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="outline"
              data-testid="custom-cancel"
              onClick={() => {
                setOpen(false)
              }}
            >
              Cancel
            </Button>
            <Button size="sm" data-testid="custom-apply" onClick={applyCustom}>
              Apply
            </Button>
          </div>
        </div>
      )}
    </div>
  )

  const trigger = (
    <Button
      size="sm"
      variant="outline"
      data-testid="time-range-trigger"
      onClick={() => {
        if (!isDesktop) handleOpenChange(true)
      }}
    >
      {labelForValue(value)}
      <ChevronDown className="ml-1 size-4" />
    </Button>
  )

  if (isDesktop) {
    return (
      <DropdownMenu open={open} onOpenChange={handleOpenChange}>
        <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className="w-72"
          // Keep focus inside the inputs; radix would otherwise steal it.
          onCloseAutoFocus={(e) => {
            e.preventDefault()
          }}
        >
          {panel}
        </DropdownMenuContent>
      </DropdownMenu>
    )
  }

  return (
    <>
      {trigger}
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-full sm:max-w-md">
          <DialogTitle>Select time range</DialogTitle>
          {panel}
        </DialogContent>
      </Dialog>
    </>
  )
}
