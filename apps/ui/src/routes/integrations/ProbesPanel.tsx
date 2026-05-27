import { EmptyState } from '@/components/EmptyState'
import { useListContainers } from '@/api/docker'

import { ContainerProbesCard } from './ContainerProbesCard'

/**
 * STAGE-003-012 Refinement scope expansion (2026-05-26):
 * One card per container in inventory. The card shows the container's
 * active probes (with Edit/Delete) + suggested defaults from
 * docker inspect that aren't yet in probe_targets (with Add/Ignore).
 *
 * Replaces the per-suggestion SuggestionCard flow. The legacy
 * /suggestions/{id}/{accept,customize,ignore} endpoints remain on the
 * backend for EPIC-011 to subsume but are NOT exercised from this UI.
 */
export function ProbesPanel() {
  const query = useListContainers()
  const containers = query.data?.containers ?? []

  return (
    <section className="space-y-3" aria-labelledby="probes-heading">
      <h2 id="probes-heading" className="text-base font-semibold tracking-tight">
        Probes
      </h2>

      {query.isLoading && <EmptyState testId="probes-loading">Loading…</EmptyState>}

      {query.isError && !query.isLoading && (
        <EmptyState testId="probes-error">Failed to load containers.</EmptyState>
      )}

      {!query.isLoading && !query.isError && containers.length === 0 && (
        <EmptyState testId="probes-empty">No containers in inventory.</EmptyState>
      )}

      {!query.isLoading && !query.isError && containers.length > 0 && (
        <div className="space-y-3" data-testid="probes-list">
          {containers.map((c) => (
            <ContainerProbesCard key={c.id} container={c} />
          ))}
        </div>
      )}
    </section>
  )
}
