import { useMemo } from 'react'
import { Columns3 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/** Fixed common columns — always offered, in this order. */
const COMMON_COLUMNS = ['service', 'host', 'severity'] as const

interface ColumnsControlProps {
  /** Ordered list of currently-selected column field names. */
  selected: string[]
  /** Discovered field names (from useLogsFieldsQuery). */
  available: string[]
  /** Emit the next ordered selection (append on add, filter on remove). */
  onChange: (next: string[]) => void
}

/**
 * STAGE-004-018B — column picker for the Logs Explorer results table.
 * Icon button (mirrors WrapIconToggle) opens a dropdown with two groups:
 * "Common" (service/host/severity, always shown) and "Discovered" (the passed
 * field names UNION any currently-selected field that is no longer discovered,
 * so a stale-but-selected column is still removable). Order = selection order.
 */
export function ColumnsControl({ selected, available, onChange }: ColumnsControlProps) {
  // Discovered group = (available ∪ selected) minus the Common set, deduped,
  // so a selected-but-undiscovered field still appears (checked + removable).
  const discovered = useMemo(() => {
    const commonSet = new Set<string>(COMMON_COLUMNS)
    const union = new Set<string>(available)
    for (const s of selected) union.add(s)
    return [...union].filter((f) => !commonSet.has(f)).sort((a, b) => a.localeCompare(b))
  }, [available, selected])

  const isOn = (field: string): boolean => selected.includes(field)

  const toggle = (field: string): void => {
    onChange(isOn(field) ? selected.filter((f) => f !== field) : [...selected, field])
  }

  const active = selected.length > 0

  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant={active ? 'secondary' : 'ghost'}
              className="h-8 w-8 p-0"
              data-testid="logs-columns-toggle"
              aria-label="Columns"
              aria-pressed={active}
            >
              <Columns3 />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>Columns</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="max-h-80 w-56 overflow-y-auto"
        data-testid="logs-columns-menu"
      >
        <DropdownMenuLabel>Common</DropdownMenuLabel>
        {COMMON_COLUMNS.map((field) => (
          <DropdownMenuCheckboxItem
            key={field}
            checked={isOn(field)}
            onCheckedChange={() => toggle(field)}
            onSelect={(e) => e.preventDefault()}
            data-testid="logs-column-option"
            data-field={field}
          >
            {field}
          </DropdownMenuCheckboxItem>
        ))}
        {discovered.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Discovered</DropdownMenuLabel>
            {discovered.map((field) => (
              <DropdownMenuCheckboxItem
                key={field}
                checked={isOn(field)}
                onCheckedChange={() => toggle(field)}
                onSelect={(e) => e.preventDefault()}
                data-testid="logs-column-option"
                data-field={field}
              >
                {field}
              </DropdownMenuCheckboxItem>
            ))}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
