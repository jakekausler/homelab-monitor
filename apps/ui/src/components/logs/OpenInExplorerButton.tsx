// STAGE-004-021 — shared "Open in Explorer" deep-link button. Renders a
// TanStack <Link> (SPA navigation — NO full page reload) styled as an outline
// Button, targeting the /logs Explorer with pre-filled filters + time range.
// Reused by the Docker log viewer, the Cron run log viewer, and future
// inventory detail pages (HA / Pi-hole / Synology / Unifi).

import { Link } from '@tanstack/react-router'
import { SquareArrowOutUpRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { buildExplorerUrl } from '@/lib/explorerLink'
import type { PresetToken } from '@/lib/timeRange'

export interface OpenInExplorerButtonProps {
  logsQl?: string | undefined
  plainText?: string | undefined
  selectedServices?: string[] | undefined
  sincePreset?: PresetToken | undefined
  rangeStart?: Date | undefined
  rangeEnd?: Date | undefined
  label?: string | undefined
}

export function OpenInExplorerButton({
  logsQl,
  plainText,
  selectedServices,
  sincePreset,
  rangeStart,
  rangeEnd,
  label = 'Open in Explorer',
}: OpenInExplorerButtonProps) {
  const url = buildExplorerUrl({
    logsQl,
    plainText,
    selectedServices,
    sincePreset,
    rangeStart,
    rangeEnd,
  })

  return (
    <Button asChild variant="outline" size="sm" data-testid="open-in-explorer">
      <Link to={url} aria-label={label}>
        <SquareArrowOutUpRight className="mr-1 size-4" aria-hidden="true" />
        {label}
      </Link>
    </Button>
  )
}
