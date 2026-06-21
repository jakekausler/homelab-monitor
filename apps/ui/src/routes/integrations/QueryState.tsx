import type { JSX, ReactNode } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { ApiError } from '@/api/client'
import { ErrorDisplay } from '@/components/ErrorDisplay'

// Renders the standard pending / 502 / 404 / error states; renderData runs on success.
export function QueryState<T>({
  result,
  unavailableLabel,
  notFoundLabel,
  renderData,
}: {
  result: UseQueryResult<T, ApiError>
  unavailableLabel: string
  notFoundLabel?: string
  renderData: (data: T) => ReactNode
}): JSX.Element {
  const isNotFound = result.error?.status === 404 && notFoundLabel !== undefined
  return (
    <>
      {result.isPending && <p className="text-sm text-muted-foreground">Loading…</p>}
      {result.error?.status === 502 && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          {unavailableLabel}
        </div>
      )}
      {isNotFound && (
        <div
          className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800"
          role="status"
          aria-live="polite"
        >
          {notFoundLabel}
        </div>
      )}
      {result.isError && result.error.status !== 502 && !isNotFound && (
        <ErrorDisplay error={result.error} />
      )}
      {result.data !== undefined && renderData(result.data)}
    </>
  )
}
