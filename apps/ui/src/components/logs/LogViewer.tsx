import { useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { ErrorDisplay } from '@/components/ErrorDisplay'
import { EmptyState } from '@/components/EmptyState'
import { cn } from '@/lib/utils'
import { LOG_SCROLL_CONTAINER_ATTR } from '@/lib/explorerState'
import { LogBanner } from './LogBanner'
import { LogLineList } from './LogLineList'
import type { LogLine, LogViewerProps } from './types'

const DEFAULT_EMPTY = 'No log lines.'
const DEFAULT_UNAVAILABLE = 'Logs temporarily unavailable. The Refresh button still works.'

export function LogViewer({
  useLogs,
  headerSlot,
  emptyStateCopy,
  unavailableCopy,
  wrap,
  timezone,
  fieldInspectorEnabled = false,
  onInspectLine,
  selectedKey: controlledSelectedKey,
  onLineSelected,
  fillHeight = false,
}: LogViewerProps) {
  const {
    lines,
    isLoading,
    isError,
    error,
    logStatus,
    truncated,
    hasMore,
    isLoadingOlder,
    loadOlder,
    trimmedOlder,
    trimmedNewer,
    hasNewer,
    isLoadingNewer,
    loadNewer,
  } = useLogs()

  // When a controlled selectedKey is provided (non-undefined), the parent owns
  // selection and we skip internal state. When uncontrolled (undefined), LogViewer
  // owns it as before (Docker/Cron unaffected — they never pass selectedKey).
  const isControlled = controlledSelectedKey !== undefined
  const [internalSelectedKey, setInternalSelectedKey] = useState<string | null>(null)
  const selectedKey = isControlled ? controlledSelectedKey : internalSelectedKey

  const handleLineClick = (line: LogLine, key: string): void => {
    if (isControlled) {
      if (selectedKey === key) {
        onLineSelected?.(null, null)
        onInspectLine?.(null)
      } else {
        onLineSelected?.(line, key)
        onInspectLine?.(line)
      }
    } else {
      if (internalSelectedKey === key) {
        setInternalSelectedKey(null)
        onInspectLine?.(null)
      } else {
        setInternalSelectedKey(key)
        onInspectLine?.(line)
      }
    }
  }

  return (
    <div className={cn(fillHeight ? 'flex h-full min-h-0 flex-col gap-4' : 'space-y-4')}>
      {headerSlot}

      <div
        className={cn(fillHeight && 'min-h-0 flex-1 overflow-y-auto')}
        {...(fillHeight ? { [LOG_SCROLL_CONTAINER_ATTR]: '' } : {})}
      >
        <div className={cn(fillHeight ? 'space-y-4' : 'contents')}>
          {isLoading && <p className="text-sm text-muted-foreground">Loading logs…</p>}

          {logStatus === 'unavailable' && (
            <div
              className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-800 dark:text-amber-200"
              data-testid="unavailable-banner"
              role="status"
            >
              {unavailableCopy ?? DEFAULT_UNAVAILABLE}
            </div>
          )}

          {logStatus === 'unknown' && (
            <EmptyState testId="logs-unknown">
              {unavailableCopy ?? 'Logs source not found.'}
            </EmptyState>
          )}

          {isError && logStatus !== 'unavailable' && logStatus !== 'unknown' && (
            <ErrorDisplay error={error} />
          )}

          {logStatus === 'no_lines' && (
            <EmptyState testId="no-lines">{emptyStateCopy ?? DEFAULT_EMPTY}</EmptyState>
          )}

          {logStatus === 'expired' && (
            <p className="text-sm text-muted-foreground" data-testid="expired-notice">
              Log text no longer available (past VictoriaLogs retention).
            </p>
          )}

          {(logStatus === 'available' || logStatus === 'running') && (
            <div className="space-y-2">
              {trimmedOlder === true && (
                <LogBanner tone="amber" testId="trimmed-older-banner" role="status">
                  Older lines removed — Load older to fetch more.
                </LogBanner>
              )}
              {loadOlder != null && hasMore === true && (
                <LoadOlderButton onClick={loadOlder} isLoading={isLoadingOlder ?? false} />
              )}
              {logStatus === 'running' && (
                <LogBanner tone="blue" testId="running-banner">
                  Run in progress — output as of now.
                </LogBanner>
              )}
              {truncated && (
                <LogBanner tone="amber" testId="truncated-banner" role="status">
                  Showing first {String((lines ?? []).length)} lines. Narrow the time window to see
                  all entries.
                </LogBanner>
              )}
              <LogLineList
                lines={lines ?? []}
                testId="logs-body"
                wrap={wrap ?? false}
                timezone={timezone ?? 'local'}
                {...(fieldInspectorEnabled && { onLineClick: handleLineClick })}
                selectedKey={fieldInspectorEnabled ? selectedKey : null}
              />
              {loadNewer != null && hasNewer === true && (
                <LoadNewerButton onClick={loadNewer} isLoading={isLoadingNewer ?? false} />
              )}
              {trimmedNewer === true && (
                <LogBanner tone="amber" testId="trimmed-newer-banner" role="status">
                  Newer lines removed — Load newer to fetch more.
                </LogBanner>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function LoadOlderButton({ onClick, isLoading }: { onClick: () => void; isLoading: boolean }) {
  return (
    <div className="flex justify-center">
      <Button
        size="sm"
        variant="outline"
        onClick={onClick}
        disabled={isLoading}
        aria-busy={isLoading}
        data-testid="load-older"
      >
        <ChevronUp className={isLoading ? 'mr-1 size-4 animate-spin' : 'mr-1 size-4'} />
        {isLoading ? 'Loading…' : 'Load older'}
      </Button>
    </div>
  )
}

function LoadNewerButton({ onClick, isLoading }: { onClick: () => void; isLoading: boolean }) {
  return (
    <div className="flex justify-center">
      <Button
        size="sm"
        variant="outline"
        onClick={onClick}
        disabled={isLoading}
        aria-busy={isLoading}
        data-testid="load-newer"
      >
        <ChevronDown className={isLoading ? 'mr-1 size-4 animate-spin' : 'mr-1 size-4'} />
        {isLoading ? 'Loading…' : 'Load newer'}
      </Button>
    </div>
  )
}
