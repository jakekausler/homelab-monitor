import { useMemo, useState } from 'react'
import type { JSX } from 'react'

import { useModelDetail, useModelsList, type ModelDetailResponse } from '@/api/models'
import { EmptyState } from '@/components/EmptyState'
import { formatRelative } from '@/lib/relativeTime'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { OpenInExplorerButton } from '@/components/logs/OpenInExplorerButton'
import { templateToLogsQl } from '@/lib/logsQlTranslate'

type SortKey = 'template_id' | 'size' | 'first_seen_ts'

export function ModelsDebugPage(): JSX.Element {
  const { data: list, error: listError } = useModelsList()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const { data: detail } = useModelDetail(selectedKey ?? '', selectedKey !== null)

  const [sortBy, setSortBy] = useState<SortKey>('template_id')
  const [searchText, setSearchText] = useState('')

  // Search-param deep-link: ?model=<key> pre-selects on arrival.
  // Uses the render-body prev-compare pattern (NO useEffect — build-failing rule).
  const search = useSearch({ strict: false })
  const [prevSearchModel, setPrevSearchModel] = useState<string | undefined>(undefined)
  if (search.model !== undefined && search.model !== prevSearchModel) {
    setPrevSearchModel(search.model)
    setSelectedKey(search.model)
  }

  const navigate = useNavigate()

  const models = list?.models ?? []

  // Compute sorted & filtered templates
  const filteredTemplates = useMemo(() => {
    if (!detail) return []

    const templates = detail.templates
      .filter((t) => t.template_str.toLowerCase().includes(searchText.toLowerCase()))
      .sort((a, b) => {
        switch (sortBy) {
          case 'template_id':
            return a.template_id - b.template_id
          case 'size':
            return a.size - b.size
          case 'first_seen_ts':
            return a.first_seen_ts - b.first_seen_ts
          default:
            return 0
        }
      })

    return templates
  }, [detail, sortBy, searchText])

  const countMismatch =
    detail !== undefined && detail.summary.template_count !== detail.templates.length

  // Handle drain disabled (503 or other error). Declared after all hooks so
  // the rules-of-hooks invariant holds (hooks must run on every render).
  if (listError !== null) {
    return (
      <div className="flex h-full min-h-0 flex-col items-center justify-center p-4">
        <EmptyState data-testid="drain-disabled-empty">
          Drain disabled. Drain pipeline is not enabled.
        </EmptyState>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      {/* Desktop: left sidebar + right panel */}
      <div className="hidden h-full min-h-0 w-full flex-col md:flex md:flex-row">
        {/* Left sidebar: model list */}
        <div className="flex min-h-0 w-64 flex-col border-r border-border bg-muted/30">
          <div className="flex-1 overflow-auto">
            {models.length === 0 ? (
              <div className="p-4 text-xs text-muted-foreground">No models</div>
            ) : (
              <ul className="space-y-1 p-2" data-testid="model-list-desktop">
                {models.map((model) => (
                  <li key={model.model_key}>
                    <button
                      onClick={() => setSelectedKey(model.model_key)}
                      className={`w-full rounded-md px-2 py-1.5 text-left text-sm ${
                        selectedKey === model.model_key
                          ? 'bg-card font-medium text-foreground'
                          : 'text-muted-foreground hover:bg-accent hover:text-foreground'
                      }`}
                      data-testid="model-list-item"
                      data-model-key={model.model_key}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="truncate">{model.model_key}</div>
                        <span className="shrink-0 rounded bg-primary/20 px-1.5 py-0.5 text-xs font-semibold text-primary">
                          {model.template_count}
                        </span>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Right panel: detail view */}
        <div className="flex min-h-0 flex-1 flex-col">
          {selectedKey === null ? (
            <div className="flex items-center justify-center p-4">
              <EmptyState>Select a model to view templates</EmptyState>
            </div>
          ) : detail === undefined ? (
            <div className="flex items-center justify-center p-4">
              <div className="text-sm text-muted-foreground">Loading...</div>
            </div>
          ) : (
            <RenderDetailPanel
              detail={detail}
              sortBy={sortBy}
              setSortBy={setSortBy}
              searchText={searchText}
              setSearchText={setSearchText}
              filteredTemplates={filteredTemplates}
              countMismatch={countMismatch}
              selectedKey={selectedKey}
              navigate={navigate}
            />
          )}
        </div>
      </div>

      {/* Mobile: dropdown + stacked cards */}
      <div className="flex h-full min-h-0 flex-col md:hidden">
        {/* Mobile header: dropdown */}
        <div className="flex flex-col gap-3 border-b border-border bg-muted/50 p-4">
          <select
            value={selectedKey ?? ''}
            onChange={(e) => setSelectedKey(e.target.value || null)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            data-testid="model-select-mobile"
          >
            <option value="">Select a model...</option>
            {models.map((model) => (
              <option key={model.model_key} value={model.model_key}>
                {model.model_key} ({model.template_count})
              </option>
            ))}
          </select>

          {selectedKey !== null && (
            <>
              <input
                type="text"
                placeholder="Search templates..."
                value={searchText}
                onChange={(e) => setSearchText(e.currentTarget.value)}
                className="rounded-md border border-border bg-background px-2 py-1 text-sm"
                data-testid="model-search"
              />
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as SortKey)}
                className="rounded-md border border-border bg-background px-2 py-1 text-sm"
                data-testid="model-sort-mobile"
              >
                <option value="template_id">Sort by ID</option>
                <option value="size">Sort by Size</option>
                <option value="first_seen_ts">Sort by First Seen</option>
              </select>
            </>
          )}
        </div>

        {/* Mobile body: stacked cards */}
        <div className="min-h-0 flex-1 overflow-auto">
          {selectedKey === null ? (
            <div className="p-4">
              <EmptyState>Select a model to view templates</EmptyState>
            </div>
          ) : detail === undefined ? (
            <div className="p-4 text-sm text-muted-foreground">Loading...</div>
          ) : (
            <>
              {countMismatch && (
                <div className="m-2 rounded-md border-l-4 border-yellow-600 bg-yellow-50 p-2 text-xs text-yellow-800 dark:bg-yellow-900/20 dark:text-yellow-100">
                  Stored count {detail.summary.template_count} differs from live count{' '}
                  {detail.templates.length} — snapshot may be stale/corrupt
                </div>
              )}
              {/* Stats caption */}
              <p
                className="mx-2 mt-2 text-xs text-muted-foreground"
                data-testid="models-stats-caption"
              >
                Counts reflect log lines ingested by drain while it has been running — not full log
                history. <code className="font-mono">last seen</code> is shared across all templates
                in a model.
              </p>
              <ul className="space-y-2 p-2" data-testid="model-templates-cards">
                {filteredTemplates.length === 0 ? (
                  <li className="p-4 text-center text-xs text-muted-foreground">
                    No templates match
                  </li>
                ) : (
                  filteredTemplates.map((t) => (
                    <li
                      key={t.template_id}
                      className="rounded-md border border-border bg-card p-3 text-xs"
                    >
                      <div className="space-y-1">
                        <div className="font-semibold">Template {t.template_id}</div>
                        <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-1 font-mono text-xs">
                          {t.template_str}
                        </pre>
                        <div className="grid grid-cols-2 gap-1 text-muted-foreground">
                          <div>Size: {t.size}</div>
                          <div>Hash: {t.template_hash.slice(0, 8)}</div>
                          <div>
                            First: {formatRelative(new Date(t.first_seen_ts).toISOString())}
                          </div>
                          <div>Last: {formatRelative(new Date(t.last_seen_ts).toISOString())}</div>
                        </div>
                        <div className="pt-1">
                          <TemplateActions
                            templateStr={t.template_str}
                            templateHash={t.template_hash}
                            serviceKey={selectedKey ?? ''}
                            navigate={navigate}
                          />
                        </div>
                      </div>
                    </li>
                  ))
                )}
              </ul>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

interface TemplateActionsProps {
  templateStr: string
  templateHash: string
  serviceKey: string
  navigate: ReturnType<typeof useNavigate>
}

/** Per-template actions: Open in Explorer (from raw template_str) + View signature. */
function TemplateActions({
  templateStr,
  templateHash,
  serviceKey,
  navigate,
}: TemplateActionsProps): JSX.Element {
  const logsQl = templateToLogsQl(templateStr)
  return (
    <div className="flex items-center gap-2">
      {logsQl.length > 0 && <OpenInExplorerButton logsQl={logsQl} />}
      <button
        type="button"
        onClick={() => {
          void navigate({
            to: '/logs/signatures/$templateHash/$serviceKey',
            params: { templateHash, serviceKey },
          })
        }}
        className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent"
        data-testid="view-signature-link"
      >
        View signature
      </button>
    </div>
  )
}

function RenderDetailPanel({
  detail,
  sortBy,
  setSortBy,
  searchText,
  setSearchText,
  filteredTemplates,
  countMismatch,
  selectedKey,
  navigate,
}: {
  detail: ModelDetailResponse
  sortBy: SortKey
  setSortBy: (key: SortKey) => void
  searchText: string
  setSearchText: (text: string) => void
  filteredTemplates: ModelDetailResponse['templates']
  countMismatch: boolean
  selectedKey: string
  navigate: ReturnType<typeof useNavigate>
}): JSX.Element {
  return (
    <>
      {/* Header: search + sort */}
      <div className="flex flex-wrap gap-3 border-b border-border bg-muted/50 p-4">
        <input
          type="text"
          placeholder="Search templates..."
          value={searchText}
          onChange={(e) => setSearchText(e.currentTarget.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          data-testid="model-search"
        />
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as SortKey)}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          data-testid="model-sort"
        >
          <option value="template_id">Sort by ID</option>
          <option value="size">Sort by Size</option>
          <option value="first_seen_ts">Sort by First Seen</option>
        </select>
      </div>

      {/* Count mismatch warning */}
      {countMismatch && (
        <div className="m-4 rounded-md border-l-4 border-yellow-600 bg-yellow-50 p-2 text-xs text-yellow-800 dark:bg-yellow-900/20 dark:text-yellow-100">
          Stored count {detail.summary.template_count} differs from live count{' '}
          {detail.templates.length} — snapshot may be stale/corrupt
        </div>
      )}

      {/* Stats caption */}
      <p className="mx-4 mt-2 text-xs text-muted-foreground" data-testid="models-stats-caption">
        Counts reflect log lines ingested by drain while it has been running — not full log history.{' '}
        <code className="font-mono">last seen</code> is shared across all templates in a model.
      </p>

      {/* Table */}
      <div className="min-h-0 flex-1 overflow-auto">
        {filteredTemplates.length === 0 ? (
          <div className="p-4 text-center text-sm text-muted-foreground">
            No templates match filter
          </div>
        ) : (
          <table className="w-full border-collapse text-sm" data-testid="model-templates-table">
            <thead className="sticky top-0 z-10 bg-background/95 backdrop-blur">
              <tr className="border-b border-border">
                <th className="px-4 py-2 text-left font-semibold">Template ID</th>
                <th className="px-4 py-2 text-left font-semibold">Template</th>
                <th className="px-4 py-2 text-right font-semibold">Size</th>
                <th className="px-4 py-2 text-left font-semibold">First Seen</th>
                <th className="px-4 py-2 text-left font-semibold">Last Seen</th>
                <th className="px-4 py-2 text-left font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredTemplates.map((t) => (
                <tr key={t.template_id} className="border-b border-border hover:bg-muted/30">
                  <td className="px-4 py-2">{t.template_id}</td>
                  <td className="px-4 py-2">
                    <pre className="max-w-md overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">
                      {t.template_str}
                    </pre>
                  </td>
                  <td className="px-4 py-2 text-right">{t.size}</td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {formatRelative(new Date(t.first_seen_ts).toISOString())}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {formatRelative(new Date(t.last_seen_ts).toISOString())}
                  </td>
                  <td className="px-4 py-2">
                    <TemplateActions
                      templateStr={t.template_str}
                      templateHash={t.template_hash}
                      serviceKey={selectedKey}
                      navigate={navigate}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
