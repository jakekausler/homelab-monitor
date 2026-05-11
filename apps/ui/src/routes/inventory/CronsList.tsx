import { useMemo, useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'

import { useListCrons } from '@/api/crons'
import { AddCronModal } from '@/components/crons/AddCronModal'
import { CronsTable } from '@/components/crons/CronsTable'
import { CronsToolbar, type ToolbarFilters } from '@/components/crons/CronsToolbar'
import { Button } from '@/components/ui/button'

export function CronsListPage() {
  const search = useSearch({ from: '/protected/inventory/crons' })
  const navigate = useNavigate()
  const [addOpen, setAddOpen] = useState(false)

  const filters: ToolbarFilters = {
    ...(search.host !== undefined && { host: search.host }),
    ...(search.integration_mode !== undefined && { integration_mode: search.integration_mode }),
    ...(search.state !== undefined && { state: search.state }),
    ...(search.enabled !== undefined && { enabled: search.enabled }),
    ...(search.q !== undefined && { q: search.q }),
    include_archived: search.include_archived ?? false,
  }

  const list = useListCrons({
    page: search.page ?? 1,
    page_size: search.page_size ?? 100,
    ...(filters.host !== undefined && { host: filters.host }),
    ...(filters.integration_mode !== undefined && { integration_mode: filters.integration_mode }),
    ...(filters.state !== undefined && { state: filters.state }),
    ...(filters.enabled !== undefined && { enabled: filters.enabled }),
    ...(filters.q !== undefined && { q: filters.q }),
    include_archived: filters.include_archived,
  })

  const knownHosts = useMemo(() => {
    const items = list.data?.items ?? []
    return Array.from(new Set(items.map((c) => c.host))).sort()
  }, [list.data])

  const handleFiltersChange = (next: ToolbarFilters) => {
    void navigate({
      to: '/inventory/crons',
      search: {
        page: 1,
        page_size: search.page_size ?? 100,
        ...(next.host !== undefined && { host: next.host }),
        ...(next.integration_mode !== undefined && { integration_mode: next.integration_mode }),
        ...(next.state !== undefined && { state: next.state }),
        ...(next.enabled !== undefined && { enabled: next.enabled }),
        ...(next.q !== undefined && { q: next.q }),
        include_archived: next.include_archived,
      },
    })
  }

  const total = list.data?.total ?? 0
  const items = list.data?.items ?? []

  return (
    <div className="space-y-4">
      <CronsToolbar
        filters={filters}
        knownHosts={knownHosts}
        onFiltersChange={handleFiltersChange}
        onAddClick={() => setAddOpen(true)}
      />

      {list.error && (
        <p role="alert" className="text-red-600">
          {list.error.message}
        </p>
      )}

      <CronsTable items={items} isLoading={list.isLoading} />

      {total > items.length && (
        <Pagination
          page={search.page ?? 1}
          pageSize={search.page_size ?? 100}
          total={total}
          onPageChange={(p) =>
            void navigate({
              to: '/inventory/crons',
              search: {
                page: p,
                page_size: search.page_size ?? 100,
                ...(search.host !== undefined && { host: search.host }),
                ...(search.integration_mode !== undefined && {
                  integration_mode: search.integration_mode,
                }),
                ...(search.state !== undefined && { state: search.state }),
                ...(search.enabled !== undefined && { enabled: search.enabled }),
                ...(search.q !== undefined && { q: search.q }),
                include_archived: search.include_archived ?? false,
              },
            })
          }
        />
      )}

      <AddCronModal open={addOpen} onOpenChange={setAddOpen} />
    </div>
  )
}

function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
}: {
  page: number
  pageSize: number
  total: number
  onPageChange: (p: number) => void
}) {
  const lastPage = Math.max(1, Math.ceil(total / pageSize))
  return (
    <div className="flex items-center justify-end gap-3 text-sm">
      <Button
        variant="outline"
        size="sm"
        onClick={() => onPageChange(Math.max(1, page - 1))}
        disabled={page <= 1}
      >
        Previous
      </Button>
      <span className="text-muted-foreground">
        Page {page} of {lastPage} ({total} crons)
      </span>
      <Button
        variant="outline"
        size="sm"
        onClick={() => onPageChange(Math.min(lastPage, page + 1))}
        disabled={page >= lastPage}
      >
        Next
      </Button>
    </div>
  )
}
