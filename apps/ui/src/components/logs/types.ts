import type { ReactNode } from 'react'

import type { ApiError } from '@/api/client'
import type { Schema } from '@/api/types'

/** The converged log line surfaced by every log endpoint (STAGE-004-002). */
export type LogLine = Schema<'LogLine'>

/** Normalized status enum the convenience <LogViewer> understands. */
export type LogViewerStatus =
  | 'available'
  | 'no_lines'
  | 'unavailable'
  | 'unknown'
  | 'expired'
  | 'running'

/** Banner color tones. */
export type LogBannerTone = 'amber' | 'blue'

/** The data shape a caller's useLogs() hook must return for <LogViewer>. */
export interface UseLogsResult {
  lines: LogLine[] | undefined
  isLoading: boolean
  isError: boolean
  error: ApiError | undefined
  logStatus?: LogViewerStatus | undefined
  truncated?: boolean | undefined
  /** STAGE-004-007: cursor pagination (all optional — existing callers unaffected). */
  hasMore?: boolean | undefined
  isLoadingOlder?: boolean | undefined
  loadOlder?: (() => void) | undefined
  /** STAGE-004-024: bidirectional windowed pager. */
  trimmedOlder?: boolean | undefined
  trimmedNewer?: boolean | undefined
  hasNewer?: boolean | undefined
  isLoadingNewer?: boolean | undefined
  loadNewer?: (() => void) | undefined
}

export interface LogViewerProps {
  useLogs: () => UseLogsResult
  headerSlot?: ReactNode
  emptyStateCopy?: string
  unavailableCopy?: string
  /** STAGE-004-009: UTC vs configured-local timestamp rendering. Default 'local'. */
  timezone?: 'local' | 'utc'
  /** Soft-wrap long log lines. Default false. */
  wrap?: boolean
  /** STAGE-004-016: opt-in field inspector. When true, log rows become
   *  clickable and selection is emitted via onInspectLine. Default false. */
  fieldInspectorEnabled?: boolean
  /** STAGE-004-016: notified with the inspected line (or null when closed/
   *  deselected). The parent renders the panel. */
  onInspectLine?: (line: LogLine | null) => void
  /** STAGE-004-016 fix: controlled selected key. When provided (non-undefined),
   *  LogViewer uses this for highlight instead of internal state. The parent
   *  is then the single owner of selection and must clear it on close. */
  selectedKey?: string | null
  /** STAGE-004-016 fix: like onInspectLine but also receives the row key so
   *  the parent can track selection as a single { key, line } unit. When
   *  provided, supersedes onInspectLine. */
  onLineSelected?: (line: LogLine | null, key: string | null) => void
  /** STAGE-004-016 refinement: when true, LogViewer fills its parent's height
   *  and scrolls its results region internally (header slot stays static). The
   *  results region gets data-log-scroll-container for STAGE-015 scroll targeting.
   *  Default false → legacy page-level scrolling (Docker/cron viewers unchanged). */
  fillHeight?: boolean
  /** STAGE-004-018B: ordered extra-column field names for the results table.
   *  Threaded straight to LogLineList. Absent/empty → 2-column rows (back-compat). */
  columns?: string[] | undefined
}
