import { EmptyState } from '@/components/EmptyState'
import { useListDockerSuggestions } from '@/api/docker'

import { SuggestionCard } from './SuggestionCard'

export function PendingSuggestionsPanel() {
  const query = useListDockerSuggestions('pending')
  const pages = query.data?.pages ?? []
  const suggestions = pages.flatMap((p) => p.suggestions ?? [])
  const total = suggestions.length
  const isLoading = query.isLoading
  const isError = query.isError

  return (
    <section className="space-y-3" aria-labelledby="pending-suggestions-heading">
      <h2 id="pending-suggestions-heading" className="text-base font-semibold tracking-tight">
        Pending suggestions ({total})
      </h2>

      {isLoading && <EmptyState testId="pending-suggestions-loading">Loading…</EmptyState>}

      {isError && !isLoading && (
        <EmptyState testId="pending-suggestions-error">
          Failed to load pending suggestions.
        </EmptyState>
      )}

      {!isLoading && !isError && total === 0 && (
        <EmptyState testId="pending-suggestions-empty">No pending suggestions.</EmptyState>
      )}

      {!isLoading && !isError && total > 0 && (
        <div className="space-y-2">
          {suggestions.map((s) => (
            <SuggestionCard key={s.id} suggestion={s} />
          ))}
          {query.hasNextPage && (
            <button
              type="button"
              onClick={() => {
                void query.fetchNextPage()
              }}
              disabled={query.isFetchingNextPage}
              className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="suggestions-load-more"
            >
              {query.isFetchingNextPage ? 'Loading…' : 'Load more'}
            </button>
          )}
        </div>
      )}
    </section>
  )
}
