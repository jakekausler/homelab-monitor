import { useMemo, useState } from 'react'

import { useLogsFieldsQuery } from '@/api/logs'

interface FieldsDiscoveryPanelProps {
  /** Effective LogsQL expr (already mode-resolved by the Body). */
  expr: string
  /** Resolved window start (ISO Z). */
  start: string
  /** Resolved window end (ISO Z). */
  end: string
  /** Selected-services CSV (<source_type>:<service>), same as useLogsQuery. */
  services: string
  /** Inject a structured field:"value" clause (chip click). */
  onAddFieldFilter: (field: string, value: string) => void
  /** Optional: surface a field in the inspector (field-name click). */
  onSelectField?: (field: string) => void
}

function coveragePct(coverage: number): string {
  return `${Math.round(coverage * 100)}%`
}

export function FieldsDiscoveryPanel({
  expr,
  start,
  end,
  services,
  onAddFieldFilter,
  onSelectField,
}: FieldsDiscoveryPanelProps) {
  const query = useLogsFieldsQuery(expr, start, end, services)
  const [nameFilter, setNameFilter] = useState('')

  const allFields = query.data?.fields ?? []
  const needle = nameFilter.trim().toLowerCase()
  const fields = useMemo(
    () =>
      needle.length > 0
        ? allFields.filter((f) => f.name.toLowerCase().includes(needle))
        : allFields,
    [allFields, needle],
  )

  return (
    <div
      data-testid="fields-discovery"
      className="flex w-full flex-col gap-1"
      role="group"
      aria-label="Available fields"
    >
      <div className="px-2 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Available fields
      </div>

      <input
        type="text"
        data-testid="fields-discovery-search"
        aria-label="Filter fields by name"
        placeholder="Filter fields…"
        value={nameFilter}
        onChange={(e) => setNameFilter(e.target.value)}
        className="mx-2 mb-1 flex h-8 rounded-md border border-input bg-background px-2 text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />

      {query.isLoading && (
        <div
          data-testid="fields-discovery-loading"
          className="px-2 py-2 text-sm text-muted-foreground"
        >
          Loading fields…
        </div>
      )}

      {!query.isLoading && query.isError && (
        <div
          data-testid="fields-discovery-error"
          role="alert"
          className="px-2 py-2 text-sm text-red-600"
        >
          Failed to load fields.
        </div>
      )}

      {!query.isLoading && !query.isError && fields.length === 0 && (
        <div
          data-testid="fields-discovery-empty"
          className="px-2 py-2 text-sm text-muted-foreground"
        >
          No fields in this scope.
        </div>
      )}

      {!query.isLoading &&
        !query.isError &&
        fields.map((f) => (
          <div
            key={f.name}
            data-testid="fields-discovery-row"
            data-field={f.name}
            className="flex flex-col gap-1 px-2 py-1.5"
          >
            <div className="flex items-center gap-2">
              {onSelectField !== undefined ? (
                <button
                  type="button"
                  data-testid="fields-discovery-name"
                  data-field={f.name}
                  onClick={() => onSelectField(f.name)}
                  className="min-w-0 flex-1 truncate text-left font-mono text-xs text-foreground hover:underline"
                  title={f.name}
                >
                  {f.name}
                </button>
              ) : (
                <span
                  data-testid="fields-discovery-name"
                  data-field={f.name}
                  className="min-w-0 flex-1 truncate font-mono text-xs text-foreground"
                  title={f.name}
                >
                  {f.name}
                </span>
              )}
              <span
                data-testid="fields-discovery-coverage"
                className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground"
              >
                {coveragePct(f.coverage)}
              </span>
              <span
                data-testid="fields-discovery-type"
                className="shrink-0 text-[10px] uppercase text-muted-foreground"
              >
                {f.type_hint}
              </span>
            </div>
            {f.sample_values.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {f.sample_values.map((v) => (
                  <button
                    key={v}
                    type="button"
                    data-testid="fields-discovery-chip"
                    data-field={f.name}
                    data-value={v}
                    onClick={() => onAddFieldFilter(f.name, v)}
                    className="max-w-[10rem] truncate rounded-full border border-border bg-background px-2 py-0.5 text-[10px] hover:bg-accent"
                    title={v}
                  >
                    {v}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
    </div>
  )
}
