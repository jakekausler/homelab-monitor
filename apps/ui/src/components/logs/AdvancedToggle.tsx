import { Code, WrapText } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

interface AdvancedToggleProps {
  /** True = advanced (raw LogsQL) mode. */
  checked: boolean
  onChange: (next: boolean) => void
}

/**
 * Icon-only "Advanced (LogsQL)" toggle for the Logs Explorer search row.
 * Active (advanced) state shows a filled/secondary variant + aria-pressed.
 * Replaces the former checkbox; keeps data-testid="logs-advanced-toggle".
 * STAGE — control-bar redesign.
 */
export function AdvancedToggle({ checked, onChange }: AdvancedToggleProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="sm"
          variant={checked ? 'secondary' : 'ghost'}
          className="h-8 w-8 p-0"
          data-testid="logs-advanced-toggle"
          aria-label="Advanced (LogsQL)"
          aria-pressed={checked}
          onClick={() => {
            onChange(!checked)
          }}
        >
          <Code />
        </Button>
      </TooltipTrigger>
      <TooltipContent>Advanced (LogsQL)</TooltipContent>
    </Tooltip>
  )
}

interface WrapIconToggleProps {
  /** True = wrap log lines. */
  checked: boolean
  onChange: (next: boolean) => void
}

/**
 * Icon-only "Wrap lines" toggle for the Logs Explorer button row. Keeps
 * data-testid="wrap-toggle". Active state = secondary variant + aria-pressed.
 */
export function WrapIconToggle({ checked, onChange }: WrapIconToggleProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="sm"
          variant={checked ? 'secondary' : 'ghost'}
          className="h-8 w-8 p-0"
          data-testid="wrap-toggle"
          aria-label="Wrap lines"
          aria-pressed={checked}
          onClick={() => {
            onChange(!checked)
          }}
        >
          <WrapText />
        </Button>
      </TooltipTrigger>
      <TooltipContent>Wrap lines</TooltipContent>
    </Tooltip>
  )
}
