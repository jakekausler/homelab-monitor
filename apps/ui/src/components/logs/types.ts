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
}
