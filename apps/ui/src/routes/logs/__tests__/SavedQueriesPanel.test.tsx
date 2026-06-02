// Project test conventions:
// - Framework: Vitest (no globals — explicit imports)
// - Mocking: vi.mock() at top, vi.mocked() for typed access
// - Component render: @testing-library/react with QueryClientProvider
// - window.confirm: vi.spyOn(window, 'confirm')

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { SavedQueriesPanel } from '@/routes/logs/SavedQueriesPanel'
import type { Schema } from '@/api/types'
import type { ApiError } from '@/api/client'

type SavedQuery = Schema<'SavedQueryResponse'>
type RenameVariables = { id: number; name: string }

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------
vi.mock('@/api/savedLogQueries', () => ({
  useSavedLogQueriesQuery: vi.fn(),
  useRenameSavedLogQuery: vi.fn(),
  useDeleteSavedLogQuery: vi.fn(),
  useCreateSavedLogQuery: vi.fn(),
  useUpdateSavedLogQuery: vi.fn(),
  computeCopyName: vi.fn((name: string, existing: string[]) => {
    // Simple real-ish implementation for component-level tests
    const candidate = `${name} (copy)`
    if (!existing.includes(candidate)) return candidate
    let n = 1
    for (;;) {
      const c = `${name} (copy ${n})`
      if (!existing.includes(c)) return c
      n++
    }
  }),
  savedRowToCreateRequest: vi.fn((row: SavedQuery, newName: string) => ({
    name: newName,
    logs_ql: row.logs_ql,
    selected_services: row.selected_services,
    advanced_mode: row.advanced_mode,
    since_preset: row.since_preset ?? undefined,
  })),
}))

import {
  useSavedLogQueriesQuery,
  useRenameSavedLogQuery,
  useDeleteSavedLogQuery,
  useCreateSavedLogQuery,
} from '@/api/savedLogQueries'

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(TooltipProvider, null, children),
    )
}

function makeQuery(overrides: Partial<SavedQuery> = {}): SavedQuery {
  return {
    id: 1,
    name: 'My Query',
    logs_ql: '_msg:"error"',
    selected_services: [],
    advanced_mode: false,
    since_preset: '15m',
    range_start_iso: null,
    range_end_iso: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function mockQueryList(queries: SavedQuery[]) {
  vi.mocked(useSavedLogQueriesQuery).mockReturnValue({
    data: { saved_queries: queries },
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useSavedLogQueriesQuery>)
}

function mockMutations(
  overrides: {
    rename?: ReturnType<typeof useRenameSavedLogQuery>
    delete?: ReturnType<typeof useDeleteSavedLogQuery>
    create?: ReturnType<typeof useCreateSavedLogQuery>
  } = {},
) {
  vi.mocked(useRenameSavedLogQuery).mockReturnValue(
    overrides.rename ??
      ({
        mutate: vi.fn(),
        isPending: false,
      } as unknown as ReturnType<typeof useRenameSavedLogQuery>),
  )
  vi.mocked(useDeleteSavedLogQuery).mockReturnValue(
    overrides.delete ??
      ({
        mutate: vi.fn(),
        isPending: false,
      } as unknown as ReturnType<typeof useDeleteSavedLogQuery>),
  )
  vi.mocked(useCreateSavedLogQuery).mockReturnValue(
    overrides.create ??
      ({
        mutate: vi.fn(),
        isPending: false,
      } as unknown as ReturnType<typeof useCreateSavedLogQuery>),
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SavedQueriesPanel', () => {
  describe('loading / error / empty states', () => {
    beforeEach(() => mockMutations())

    it('shows loading state', () => {
      vi.mocked(useSavedLogQueriesQuery).mockReturnValue({
        data: undefined,
        isLoading: true,
        isError: false,
      } as unknown as ReturnType<typeof useSavedLogQueriesQuery>)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      expect(screen.getByText('Loading saved queries…')).toBeInTheDocument()
    })

    it('shows error state', () => {
      vi.mocked(useSavedLogQueriesQuery).mockReturnValue({
        data: undefined,
        isLoading: false,
        isError: true,
      } as unknown as ReturnType<typeof useSavedLogQueriesQuery>)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      expect(screen.getByText('Failed to load saved queries.')).toBeInTheDocument()
    })

    it('shows empty state when list is empty', () => {
      mockQueryList([])
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      expect(screen.getByTestId('saved-queries-empty')).toBeInTheDocument()
    })
  })

  describe('view mode (non-editing)', () => {
    beforeEach(() => {
      mockQueryList([makeQuery({ id: 1, name: 'My Query' })])
      mockMutations()
    })

    it('renders the query name as a load button', () => {
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      expect(screen.getByTestId('saved-query-load')).toHaveTextContent('My Query')
    })

    it('clicking the load button calls onLoad with the query', () => {
      const onLoad = vi.fn()
      render(<SavedQueriesPanel onLoad={onLoad} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-load'))
      expect(onLoad).toHaveBeenCalledWith(expect.objectContaining({ id: 1, name: 'My Query' }))
    })

    it('renders update, duplicate, rename, and delete action buttons', () => {
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      expect(screen.getByTestId('saved-query-update')).toBeInTheDocument()
      expect(screen.getByTestId('saved-query-duplicate')).toBeInTheDocument()
      expect(screen.getByTestId('saved-query-rename')).toBeInTheDocument()
      expect(screen.getByTestId('saved-query-delete')).toBeInTheDocument()
    })
  })

  describe('Update button', () => {
    let onUpdate: ReturnType<typeof vi.fn<(query: SavedQuery) => void>>

    beforeEach(() => {
      onUpdate = vi.fn<(query: SavedQuery) => void>()
      mockQueryList([makeQuery({ id: 1, name: 'My Query' })])
      mockMutations()
    })

    it('calls onUpdate when window.confirm returns true', () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={onUpdate} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-update'))
      expect(onUpdate).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
    })

    it('does NOT call onUpdate when window.confirm returns false', () => {
      vi.spyOn(window, 'confirm').mockReturnValue(false)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={onUpdate} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-update'))
      expect(onUpdate).not.toHaveBeenCalled()
    })
  })

  describe('Delete button', () => {
    beforeEach(() => {
      mockQueryList([makeQuery({ id: 1, name: 'My Query' })])
    })

    it('calls deleteMut.mutate when window.confirm returns true', () => {
      const deleteMutate = vi.fn()
      mockMutations({
        delete: { mutate: deleteMutate, isPending: false } as unknown as ReturnType<
          typeof useDeleteSavedLogQuery
        >,
      })
      vi.spyOn(window, 'confirm').mockReturnValue(true)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-delete'))
      expect(deleteMutate).toHaveBeenCalledWith({ id: 1 })
    })

    it('does NOT call deleteMut.mutate when window.confirm returns false', () => {
      const deleteMutate = vi.fn()
      mockMutations({
        delete: { mutate: deleteMutate, isPending: false } as unknown as ReturnType<
          typeof useDeleteSavedLogQuery
        >,
      })
      vi.spyOn(window, 'confirm').mockReturnValue(false)
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-delete'))
      expect(deleteMutate).not.toHaveBeenCalled()
    })
  })

  describe('Duplicate button', () => {
    it('calls createMut.mutate with computed copy name and mapped payload', () => {
      const query = makeQuery({ id: 1, name: 'My Query', since_preset: '1h' })
      mockQueryList([query])
      const createMutate = vi.fn()
      mockMutations({
        create: { mutate: createMutate, isPending: false } as unknown as ReturnType<
          typeof useCreateSavedLogQuery
        >,
      })

      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-duplicate'))
      // computeCopyName mock returns "My Query (copy)" since no copies exist
      expect(createMutate).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'My Query (copy)' }),
      )
    })
  })

  describe('Rename inline', () => {
    beforeEach(() => {
      mockQueryList([makeQuery({ id: 1, name: 'My Query' })])
    })

    it('clicking rename shows an input with the current name', () => {
      mockMutations()
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-rename'))
      const input = screen.getByRole<HTMLInputElement>('textbox')
      expect(input.value).toBe('My Query')
    })

    it('shows error when saving with empty name', async () => {
      mockMutations()
      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-rename'))
      const input = screen.getByRole('textbox')
      fireEvent.change(input, { target: { value: '' } })
      // Click the "Save" button inside the edit row
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => {
        expect(screen.getByText('Name is required.')).toBeInTheDocument()
      })
    })

    it('shows 409 duplicate-name error when rename returns 409', async () => {
      const { ApiError } = await import('@/api/client')
      const renameMutate = vi.fn(
        (_args: RenameVariables, { onError }: { onError: (error: ApiError) => void }) => {
          onError(
            new ApiError({
              status: 409,
              code: 'conflict',
              message: 'conflict',
              retryAfterSeconds: null,
              details: null,
            }),
          )
        },
      )
      mockMutations({
        rename: { mutate: renameMutate, isPending: false } as unknown as ReturnType<
          typeof useRenameSavedLogQuery
        >,
      })

      render(<SavedQueriesPanel onLoad={vi.fn()} onUpdate={vi.fn()} />, {
        wrapper: makeWrapper(),
      })
      fireEvent.click(screen.getByTestId('saved-query-rename'))
      const input = screen.getByRole('textbox')
      fireEvent.change(input, { target: { value: 'Other Query' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
      await waitFor(() => {
        expect(screen.getByText('A query with that name already exists.')).toBeInTheDocument()
      })
    })
  })
})
