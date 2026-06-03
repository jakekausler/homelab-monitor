import { useState } from 'react'
import { Trash2, Edit2, Save, Copy } from 'lucide-react'

import { ApiError } from '@/api/client'
import {
  useSavedLogQueriesQuery,
  useRenameSavedLogQuery,
  useDeleteSavedLogQuery,
  useCreateSavedLogQuery,
  computeCopyName,
  savedRowToCreateRequest,
  type SavedQuery,
} from '@/api/savedLogQueries'
import { Button } from '@/components/ui/button'

interface SavedQueriesPanelProps {
  /** Click a row to load it into the Explorer (page reconstructs state). */
  onLoad: (query: SavedQuery) => void
  /** Overwrite this row's payload with the current Explorer state (page-owned). */
  onUpdate: (query: SavedQuery) => void
}

export function SavedQueriesPanel({ onLoad, onUpdate }: SavedQueriesPanelProps) {
  const { data, isLoading, isError } = useSavedLogQueriesQuery()
  const renameMut = useRenameSavedLogQuery()
  const deleteMut = useDeleteSavedLogQuery()
  const createMut = useCreateSavedLogQuery()

  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [editError, setEditError] = useState<string | null>(null)

  const queries = data?.saved_queries ?? []

  const handleRenameStart = (query: SavedQuery) => {
    setEditingId(query.id)
    setEditName(query.name)
    setEditError(null)
  }

  const handleRenameSubmit = (queryId: number) => {
    const trimmedName = editName.trim()
    if (!trimmedName) {
      setEditError('Name is required.')
      return
    }

    renameMut.mutate(
      { id: queryId, name: trimmedName },
      {
        onSuccess: () => {
          setEditingId(null)
          setEditName('')
          setEditError(null)
        },
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            setEditError('A query with that name already exists.')
          } else {
            setEditError(err.message)
          }
        },
      },
    )
  }

  const handleDelete = (query: SavedQuery) => {
    if (!window.confirm(`Delete '${query.name}'?`)) return
    deleteMut.mutate({ id: query.id })
  }

  const handleUpdate = (query: SavedQuery) => {
    if (!window.confirm(`Update '${query.name}' to the current query state?`)) return
    onUpdate(query)
  }

  const handleDuplicate = (query: SavedQuery) => {
    const existingNames = queries.map((q) => q.name)
    const newName = computeCopyName(query.name, existingNames)
    const body = savedRowToCreateRequest(query, newName)
    createMut.mutate(body)
  }

  if (isLoading) {
    return <div className="p-4 text-sm text-muted-foreground">Loading saved queries…</div>
  }

  if (isError) {
    return <div className="p-4 text-sm text-red-600">Failed to load saved queries.</div>
  }

  if (queries.length === 0) {
    return (
      <div className="space-y-2 p-4" data-testid="saved-queries-empty">
        <p className="text-sm text-muted-foreground">
          No saved queries yet. Use "Save query…" to add one.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2 p-2" data-testid="saved-queries-panel">
      {queries.map((query) => (
        <div
          key={query.id}
          data-testid="saved-query-row"
          data-query-id={query.id}
          className="flex flex-col gap-1 rounded-md border border-transparent p-2 hover:bg-muted"
        >
          {editingId === query.id ? (
            // Inline edit mode
            <div className="flex-1 flex flex-col gap-1">
              <input
                type="text"
                value={editName}
                onChange={(e) => {
                  setEditName(e.target.value)
                  setEditError(null)
                }}
                className="h-8 w-full rounded border border-input bg-background px-2 py-1 text-sm"
                autoFocus
              />
              {editError && <p className="text-xs text-red-600">{editError}</p>}
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => handleRenameSubmit(query.id)}
                  disabled={renameMut.isPending}
                  className="text-xs h-7"
                >
                  {renameMut.isPending ? 'Saving…' : 'Save'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditingId(null)
                    setEditName('')
                    setEditError(null)
                  }}
                  className="text-xs h-7"
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            // View mode
            <>
              {/* Row 1: name */}
              <button
                type="button"
                onClick={() => onLoad(query)}
                data-testid="saved-query-load"
                title={query.name}
                className="w-full min-w-0 text-left text-sm font-medium hover:underline truncate"
              >
                {query.name}
              </button>
              {/* Row 2: action buttons */}
              <div className="flex items-center gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="saved-query-update"
                  onClick={() => handleUpdate(query)}
                  className="h-6 w-6 p-0"
                  aria-label={`Update ${query.name} to current state`}
                >
                  <Save className="size-3" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="saved-query-duplicate"
                  onClick={() => handleDuplicate(query)}
                  disabled={createMut.isPending}
                  className="h-6 w-6 p-0"
                  aria-label={`Duplicate ${query.name}`}
                >
                  <Copy className="size-3" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="saved-query-rename"
                  onClick={() => handleRenameStart(query)}
                  className="h-6 w-6 p-0"
                  aria-label={`Rename ${query.name}`}
                >
                  <Edit2 className="size-3" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="saved-query-delete"
                  onClick={() => handleDelete(query)}
                  disabled={deleteMut.isPending}
                  className="h-6 w-6 p-0 text-destructive"
                >
                  <Trash2 className="size-3" />
                </Button>
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  )
}
