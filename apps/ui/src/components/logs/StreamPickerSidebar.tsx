import { cn } from '@/lib/utils'
import type { Schema } from '@/api/types'

type ServiceCount = Schema<'ServiceCount'>

interface StreamPickerSidebarProps {
  services: ServiceCount[]
  truncated: boolean
  selectedServices: string[]
  onToggleService: (service: string) => void
  isLoading: boolean
  isError?: boolean
  limit?: number
  onShowMore?: () => void
}

function formatCount(n: number): string {
  return n.toLocaleString()
}

export function StreamPickerSidebar({
  services,
  truncated,
  selectedServices,
  onToggleService,
  isLoading,
  isError = false,
  onShowMore,
}: StreamPickerSidebarProps) {
  const selected = new Set(selectedServices)
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
        services.map((s) => {
          const isSelected = selected.has(s.service)
          return (
            <button
              key={s.service}
              type="button"
              data-testid="stream-picker-row"
              data-service={s.service}
              aria-pressed={isSelected}
              aria-label={`${s.service}, ${formatCount(s.count)} lines`}
              onClick={() => onToggleService(s.service)}
              className={cn(
                'flex items-center justify-between gap-3 rounded-md px-2 py-1.5 text-left text-sm text-foreground hover:bg-accent hover:text-accent-foreground',
                isSelected && 'bg-accent text-accent-foreground',
              )}
            >
              <span className="truncate">{s.service}</span>
              <span className="shrink-0 tabular-nums text-xs text-muted-foreground">
                {formatCount(s.count)}
              </span>
            </button>
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
