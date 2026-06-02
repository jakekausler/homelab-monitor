import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { cn } from '@/lib/utils'
import { formatCompactCount } from '@/lib/formatCount'
import type { Schema } from '@/api/types'
import type { ServiceIdentity } from '@/api/logs'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

type ServiceCount = Schema<'ServiceCount'>

interface StreamPickerSidebarProps {
  /** Flat per-identity counts from /api/logs/services (each has source_type). */
  services: ServiceCount[]
  truncated: boolean
  /** Currently-selected identities (service + source_type). */
  selectedIdentities: ServiceIdentity[]
  /** Toggle one identity in/out of the selection (matched by service AND source_type). */
  onToggleIdentity: (identity: ServiceIdentity) => void
  /** Bulk add the given identities to the selection (idempotent union). */
  onSelectIdentities: (identities: ServiceIdentity[]) => void
  /** Bulk remove the given identities from the selection. */
  onDeselectIdentities: (identities: ServiceIdentity[]) => void
  isLoading: boolean
  isError?: boolean
  onShowMore?: () => void
}

const FIXED_SECTION_ORDER = ['docker', 'cron', 'systemd'] as const

function sectionSortKey(sourceType: string): [number, string] {
  if (sourceType === 'unknown') return [3, ''] // always last
  const fixed = FIXED_SECTION_ORDER.indexOf(sourceType as (typeof FIXED_SECTION_ORDER)[number])
  if (fixed >= 0) return [0, String(fixed).padStart(4, '0')] // docker<cron<systemd by index
  return [1, sourceType] // other types alpha, between fixed and unknown
}

interface Section {
  sourceType: string
  rows: ServiceCount[] // this section's identities, sorted by count DESC
  totalCount: number // sum of rows' counts
}

function groupSections(services: ServiceCount[]): Section[] {
  const byType = new Map<string, ServiceCount[]>()
  for (const s of services) {
    const arr = byType.get(s.source_type) ?? []
    arr.push(s)
    byType.set(s.source_type, arr)
  }
  const sections: Section[] = []
  for (const [sourceType, rows] of byType) {
    rows.sort((a, b) => b.count - a.count || a.service.localeCompare(b.service))
    sections.push({ sourceType, rows, totalCount: rows.reduce((n, r) => n + r.count, 0) })
  }
  sections.sort((a, b) => {
    const [ka, sa] = sectionSortKey(a.sourceType)
    const [kb, sb] = sectionSortKey(b.sourceType)
    return ka - kb || sa.localeCompare(sb)
  })
  return sections
}

export function StreamPickerSidebar({
  services,
  truncated,
  selectedIdentities,
  onToggleIdentity,
  onSelectIdentities,
  onDeselectIdentities,
  isLoading,
  isError = false,
  onShowMore,
}: StreamPickerSidebarProps) {
  const keyOf = (i: ServiceIdentity) => `${i.source_type}:${i.service}`
  const selectedSet = new Set(selectedIdentities.map(keyOf))

  // STAGE-004-012A: per-section open/closed is EPHEMERAL UI state. Default OPEN.
  // STAGE-015 owns persistence — do NOT persist collapse here.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  const sections = groupSections(services)

  return (
    <div
      data-testid="stream-picker"
      className="flex w-full flex-col gap-1 overflow-y-auto"
      role="group"
      aria-label="Filter by service"
    >
      <div className="px-2 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Services
      </div>

      {isLoading && (
        <div
          data-testid="stream-picker-loading"
          className="px-2 py-2 text-sm text-muted-foreground"
        >
          Loading services…
        </div>
      )}

      {!isLoading && isError && (
        <div
          data-testid="stream-picker-error"
          role="alert"
          className="px-2 py-2 text-sm text-red-600"
        >
          Failed to load services.
        </div>
      )}

      {!isLoading && !isError && services.length === 0 && (
        <div data-testid="stream-picker-empty" className="px-2 py-2 text-sm text-muted-foreground">
          No services in this window.
        </div>
      )}

      {!isLoading &&
        !isError &&
        sections.map((section) => {
          const isCollapsed = collapsed.has(section.sourceType)

          // Count how many of this section's identities are selected
          const selectedInSection = section.rows.filter((r) =>
            selectedSet.has(keyOf({ service: r.service, source_type: r.source_type })),
          ).length

          const handleToggleCollapse = (): void => {
            setCollapsed((prev) => {
              const next = new Set(prev)
              if (next.has(section.sourceType)) {
                next.delete(section.sourceType)
              } else {
                next.add(section.sourceType)
              }
              return next
            })
          }

          const handleSelectAll = (): void => {
            if (selectedInSection === section.rows.length) {
              // All selected → deselect all
              onDeselectIdentities(
                section.rows.map((r) => ({ service: r.service, source_type: r.source_type })),
              )
            } else {
              // Some or none selected → select all
              onSelectIdentities(
                section.rows.map((r) => ({ service: r.service, source_type: r.source_type })),
              )
            }
          }

          return (
            <div key={section.sourceType}>
              {/* Section header */}
              <div
                data-testid="stream-picker-section"
                data-source-type={section.sourceType}
                className="flex items-center gap-2 px-2 py-1"
              >
                {/* Collapse toggle */}
                <button
                  type="button"
                  data-testid="stream-picker-section-toggle"
                  onClick={handleToggleCollapse}
                  className="flex shrink-0 items-center justify-center text-muted-foreground hover:text-foreground"
                  aria-label={`${isCollapsed ? 'Expand' : 'Collapse'} ${section.sourceType}`}
                >
                  {isCollapsed ? (
                    <ChevronRight className="size-4" />
                  ) : (
                    <ChevronDown className="size-4" />
                  )}
                </button>

                {/* Section label + count */}
                <span className="flex-1 text-xs font-medium uppercase text-muted-foreground">
                  {section.sourceType} ({formatCompactCount(section.totalCount)})
                </span>

                {/* Select-all/none tri-state checkbox */}
                <button
                  type="button"
                  data-testid="stream-picker-section-selectall"
                  onClick={handleSelectAll}
                  className="flex shrink-0 items-center justify-center rounded border border-border bg-background hover:bg-accent"
                  aria-checked={
                    selectedInSection === 0
                      ? 'false'
                      : selectedInSection === section.rows.length
                        ? 'true'
                        : 'mixed'
                  }
                  role="checkbox"
                  aria-label={`${
                    selectedInSection === section.rows.length ? 'Deselect' : 'Select'
                  } all ${section.sourceType} services`}
                >
                  <input
                    type="checkbox"
                    className="size-3.5 cursor-pointer"
                    checked={selectedInSection > 0}
                    ref={(el) => {
                      if (el) {
                        el.indeterminate =
                          selectedInSection > 0 && selectedInSection < section.rows.length
                      }
                    }}
                    tabIndex={-1}
                    aria-hidden="true"
                  />
                </button>
              </div>

              {/* Section rows */}
              {!isCollapsed &&
                section.rows.map((r) => {
                  const identity: ServiceIdentity = {
                    service: r.service,
                    source_type: r.source_type,
                  }
                  const isSelected = selectedSet.has(keyOf(identity))
                  return (
                    <Tooltip key={`${r.source_type}:${r.service}`}>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          data-testid="stream-picker-row"
                          data-service={r.service}
                          data-source-type={r.source_type}
                          aria-pressed={isSelected}
                          aria-label={`${r.service}, ${formatCompactCount(r.count)} lines`}
                          onClick={() => onToggleIdentity(identity)}
                          className={cn(
                            'flex min-w-0 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-foreground hover:bg-accent hover:text-accent-foreground',
                            isSelected && 'bg-accent text-accent-foreground',
                          )}
                        >
                          <span className="w-10 shrink-0 text-right tabular-nums text-xs text-muted-foreground">
                            {formatCompactCount(r.count)}
                          </span>
                          <span className="min-w-0 flex-1 truncate">{r.service}</span>
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="right">{r.service}</TooltipContent>
                    </Tooltip>
                  )
                })}
            </div>
          )
        })}

      {!isLoading && !isError && truncated && onShowMore && (
        <button
          type="button"
          data-testid="stream-picker-truncated"
          onClick={onShowMore}
          className="px-2 py-1.5 text-left text-xs text-muted-foreground hover:text-foreground"
        >
          Showing top results — show more
        </button>
      )}

      {!isLoading && !isError && truncated && !onShowMore && (
        <p
          data-testid="stream-picker-truncated"
          className="px-2 py-1.5 text-left text-xs text-muted-foreground"
        >
          Showing top results
        </p>
      )}
    </div>
  )
}
