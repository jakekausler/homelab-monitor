import { useCallback } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'
import type { JSX } from 'react'

import { useSignaturesQuery, type SignatureFilter } from '@/api/signatures'
import { EmptyState } from '@/components/EmptyState'
import { formatRelative } from '@/lib/relativeTime'

export function SignaturesTab(): JSX.Element {
  const search = useSearch({ from: '/protected/logs/signatures' })
  const navigate = useNavigate()

  // Build the filter from search params
  const filter: SignatureFilter = {
    service: typeof search.service === 'string' ? search.service : undefined,
    status:
      search.status === 'active' || search.status === 'suppressed' || search.status === 'expected'
        ? search.status
        : undefined,
    label_q: typeof search.label_q === 'string' ? search.label_q : undefined,
    limit: 100,
    offset: 0,
  }

  const { data, isLoading, error } = useSignaturesQuery(filter)

  const handleFilterChange = useCallback(
    (updates: Partial<SignatureFilter>) => {
      const next: {
        service?: string
        status?: 'active' | 'suppressed' | 'expected'
        label_q?: string
      } = {}
      const newService = 'service' in updates ? updates.service : search.service
      const newStatus = 'status' in updates ? updates.status : search.status
      const newLabelQ = 'label_q' in updates ? updates.label_q : search.label_q

      if (newService !== undefined) next.service = newService
      if (newStatus !== undefined) next.status = newStatus
      if (newLabelQ !== undefined) next.label_q = newLabelQ

      void navigate({ to: '/logs/signatures', search: next })
    },
    [search, navigate],
  )

  const handleServiceChange = (value: string) => {
    handleFilterChange({ service: value.length > 0 ? value : undefined })
  }

  const handleStatusChange = (status: 'active' | 'suppressed' | 'expected' | undefined) => {
    handleFilterChange({ status })
  }

  const handleLabelQChange = (value: string) => {
    handleFilterChange({ label_q: value.length > 0 ? value : undefined })
  }

  const handleRowClick = useCallback(
    (templateHash: string, serviceKey: string) => {
      void navigate({
        to: '/logs/signatures/$templateHash/$serviceKey',
        params: { templateHash, serviceKey },
      })
    },
    [navigate],
  )

  if (error !== null) {
    return <div className="p-4 text-sm text-destructive">Error loading signatures</div>
  }

  const signatures = data?.signatures ?? []

  const statusBadge = (status: string): JSX.Element => (
    <span
      className={`inline-block px-2 py-1 rounded text-xs font-medium ${
        status === 'active'
          ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100'
          : status === 'suppressed'
            ? 'bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-100'
            : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100'
      }`}
    >
      {status}
    </span>
  )

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Filters */}
      <div className="flex flex-wrap gap-3 border-b border-border bg-muted/50 p-4">
        <input
          type="text"
          placeholder="Filter by service..."
          value={search.service ?? ''}
          onChange={(e) => handleServiceChange(e.currentTarget.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
        />
        <select
          value={search.status ?? ''}
          onChange={(e) =>
            handleStatusChange(
              e.currentTarget.value === ''
                ? undefined
                : (e.currentTarget.value as 'active' | 'suppressed' | 'expected'),
            )
          }
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="suppressed">Suppressed</option>
          <option value="expected">Expected</option>
        </select>
        <input
          type="text"
          placeholder="Filter by label..."
          value={search.label_q ?? ''}
          onChange={(e) => handleLabelQChange(e.currentTarget.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
        />
      </div>

      {/* Table / Cards or Empty State */}
      <div className="min-h-0 flex-1 overflow-auto">
        {isLoading ? (
          <div className="p-4 text-sm text-muted-foreground">Loading signatures...</div>
        ) : signatures.length === 0 ? (
          <div className="p-4">
            <EmptyState>No signatures yet — they appear after the drain consumer runs.</EmptyState>
          </div>
        ) : (
          <>
            {/* Desktop table */}
            <div className="hidden md:block">
              <table className="w-full border-collapse text-sm" data-testid="signatures-table">
                <thead className="sticky top-0 z-10 bg-background/95 backdrop-blur">
                  <tr className="border-b border-border">
                    <th className="px-4 py-2 text-left font-semibold">Service</th>
                    <th className="px-4 py-2 text-left font-semibold">Template</th>
                    <th className="px-4 py-2 text-right font-semibold">Count</th>
                    <th className="px-4 py-2 text-left font-semibold">Last Seen</th>
                    <th className="px-4 py-2 text-left font-semibold">Label</th>
                    <th className="px-4 py-2 text-left font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {signatures.map((sig) => (
                    <tr
                      key={`${sig.template_hash}-${sig.service_key}`}
                      className="border-b border-border hover:bg-muted/30 cursor-pointer"
                      data-testid="signature-row"
                      data-template-hash={sig.template_hash}
                      data-service-key={sig.service_key}
                      onClick={() => handleRowClick(sig.template_hash, sig.service_key)}
                    >
                      <td className="px-4 py-2">{sig.service_key}</td>
                      <td className="px-4 py-2 max-w-sm truncate" title={sig.template_str}>
                        {sig.template_str}
                      </td>
                      <td className="px-4 py-2 text-right">{sig.total_count}</td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">
                        {formatRelative(new Date(sig.last_seen_at).toISOString())}
                      </td>
                      <td className="px-4 py-2">{sig.label ?? '—'}</td>
                      <td className="px-4 py-2">{statusBadge(sig.status)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile cards */}
            <ul className="space-y-2 p-2 md:hidden" data-testid="signatures-cards">
              {signatures.map((sig) => (
                <li
                  key={`${sig.template_hash}-${sig.service_key}`}
                  className="rounded-md border border-border bg-card p-3 text-sm cursor-pointer"
                  data-testid="signature-card"
                  data-template-hash={sig.template_hash}
                  data-service-key={sig.service_key}
                  onClick={() => handleRowClick(sig.template_hash, sig.service_key)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="font-medium">{sig.service_key}</div>
                    {statusBadge(sig.status)}
                  </div>
                  <div className="mt-1 space-y-1 text-xs text-muted-foreground">
                    <div className="truncate" title={sig.template_str}>
                      {sig.template_str}
                    </div>
                    <div>Count: {sig.total_count}</div>
                    <div>Last seen: {formatRelative(new Date(sig.last_seen_at).toISOString())}</div>
                    <div>Label: {sig.label ?? '—'}</div>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}
