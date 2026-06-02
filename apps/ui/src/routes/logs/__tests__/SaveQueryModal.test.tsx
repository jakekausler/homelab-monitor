// Project test conventions:
// - Framework: Vitest (no globals — explicit imports)
// - Mocking: vi.mock() at top, vi.mocked() for typed access
// - Component render: @testing-library/react with QueryClientProvider
// - No eslint-disable; vi.fn typed properly

import {
  type MutateOptions,
  type MutationFunctionContext,
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApiError } from '@/api/client'
import type { SavedQuery, SaveQueryCreateRequest } from '@/api/savedLogQueries'

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/savedLogQueries', () => ({
  useCreateSavedLogQuery: vi.fn(),
}))

import { useCreateSavedLogQuery } from '@/api/savedLogQueries'
import { SaveQueryModal } from '@/routes/logs/SaveQueryModal'

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeWrapper(): ({ children }: { children: ReactNode }) => ReactNode {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children)
}

const MOCK_SAVED: SavedQuery = {
  id: 1,
  name: 'nginx errors',
  logs_ql: '_msg:"error"',
  selected_services: [{ service: 'nginx', source_type: 'docker' }],
  advanced_mode: false,
  since_preset: '15m',
  range_start_iso: null,
  range_end_iso: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function mockCreate(
  overrides: Partial<ReturnType<typeof useCreateSavedLogQuery>> = {},
): ReturnType<typeof useCreateSavedLogQuery> {
  return {
    mutate: vi.fn(),
    isPending: false,
    isSuccess: false,
    isError: false,
    isIdle: true,
    data: undefined,
    error: null,
    ...overrides,
  } as unknown as ReturnType<typeof useCreateSavedLogQuery>
}

function defaultBuildPayload(name: string): SaveQueryCreateRequest {
  return {
    name,
    logs_ql: '_msg:"error"',
    selected_services: [{ service: 'nginx', source_type: 'docker' }],
    advanced_mode: false,
    since_preset: '15m',
  }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SaveQueryModal', () => {
  beforeEach(() => {
    vi.mocked(useCreateSavedLogQuery).mockReturnValue(mockCreate())
  })

  it('renders the modal with title and name input when open=true', () => {
    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )
    expect(screen.getByText('Save Query')).toBeInTheDocument()
    expect(screen.getByTestId('save-query-name')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })

  it('name input onChange updates the field value', () => {
    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )
    const input = screen.getByTestId<HTMLInputElement>('save-query-name')
    fireEvent.change(input, { target: { value: 'my new query' } })
    expect(input.value).toBe('my new query')
  })

  it('handleSubmit calls createMut.mutate with payload built from trimmed name', async () => {
    const mutateFn = vi.fn()
    vi.mocked(useCreateSavedLogQuery).mockReturnValue(mockCreate({ mutate: mutateFn }))

    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )

    fireEvent.change(screen.getByTestId('save-query-name'), { target: { value: '  my query  ' } })
    fireEvent.click(screen.getByTestId('save-query-submit'))

    await waitFor(() => expect(mutateFn).toHaveBeenCalledTimes(1))
    const [calledPayload] = mutateFn.mock.calls[0] as [SaveQueryCreateRequest]
    expect(calledPayload).toMatchObject({ name: 'my query' })
  })

  it('shows required error when submitting with empty name', async () => {
    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )
    fireEvent.click(screen.getByTestId('save-query-submit'))
    await waitFor(() => {
      expect(screen.getByTestId('save-query-error')).toHaveTextContent('Name is required.')
    })
  })

  it('mutate onSuccess closes modal via onOpenChange(false) and calls onSaved', async () => {
    const onOpenChange = vi.fn()
    const onSaved = vi.fn<(saved: SavedQuery) => void>()

    const mutateFn = vi.fn(
      (
        _payload: SaveQueryCreateRequest,
        options?: MutateOptions<SavedQuery, ApiError, SaveQueryCreateRequest>,
      ) => {
        const ctx: MutationFunctionContext = { client: new QueryClient(), meta: {} }
        options?.onSuccess?.(MOCK_SAVED, _payload, ctx, ctx)
      },
    )
    vi.mocked(useCreateSavedLogQuery).mockReturnValue(mockCreate({ mutate: mutateFn }))

    render(
      <SaveQueryModal
        open={true}
        onOpenChange={onOpenChange}
        buildPayload={defaultBuildPayload}
        onSaved={onSaved}
      />,
      { wrapper: makeWrapper() },
    )

    fireEvent.change(screen.getByTestId('save-query-name'), { target: { value: 'nginx errors' } })
    fireEvent.click(screen.getByTestId('save-query-submit'))

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(onSaved).toHaveBeenCalledWith(MOCK_SAVED)
  })

  it('mutate onError with status 409 shows "name already exists" inline error', async () => {
    const { ApiError: ApiErrorClass } = await import('@/api/client')

    const mutateFn = vi.fn(
      (
        _payload: SaveQueryCreateRequest,
        options?: MutateOptions<SavedQuery, ApiError, SaveQueryCreateRequest>,
      ) => {
        options?.onError?.(
          new ApiErrorClass({
            status: 409,
            code: 'conflict',
            message: 'conflict',
            retryAfterSeconds: null,
            details: null,
          }),
          _payload,
          { client: new QueryClient(), meta: {} },
          { client: new QueryClient(), meta: {} },
        )
      },
    )
    vi.mocked(useCreateSavedLogQuery).mockReturnValue(mockCreate({ mutate: mutateFn }))

    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )

    fireEvent.change(screen.getByTestId('save-query-name'), { target: { value: 'existing name' } })
    fireEvent.click(screen.getByTestId('save-query-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('save-query-error')).toHaveTextContent(
        'A query with that name already exists.',
      )
    })
  })

  it('mutate onError with non-409 status shows the error message', async () => {
    const { ApiError: ApiErrorClass } = await import('@/api/client')

    const mutateFn = vi.fn(
      (
        _payload: SaveQueryCreateRequest,
        options?: MutateOptions<SavedQuery, ApiError, SaveQueryCreateRequest>,
      ) => {
        options?.onError?.(
          new ApiErrorClass({
            status: 500,
            code: 'server_error',
            message: 'Internal server error',
            retryAfterSeconds: null,
            details: null,
          }),
          _payload,
          { client: new QueryClient(), meta: {} },
          { client: new QueryClient(), meta: {} },
        )
      },
    )
    vi.mocked(useCreateSavedLogQuery).mockReturnValue(mockCreate({ mutate: mutateFn }))

    render(
      <SaveQueryModal open={true} onOpenChange={vi.fn()} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )

    fireEvent.change(screen.getByTestId('save-query-name'), { target: { value: 'some name' } })
    fireEvent.click(screen.getByTestId('save-query-submit'))

    await waitFor(() => {
      expect(screen.getByTestId('save-query-error')).toHaveTextContent('Internal server error')
    })
  })

  it('Cancel button onClick calls onOpenChange(false)', () => {
    const onOpenChange = vi.fn()
    render(
      <SaveQueryModal open={true} onOpenChange={onOpenChange} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('handleOpenChange calls onOpenChange when closing', () => {
    const onOpenChange = vi.fn()
    render(
      <SaveQueryModal open={true} onOpenChange={onOpenChange} buildPayload={defaultBuildPayload} />,
      { wrapper: makeWrapper() },
    )
    // Cancel triggers handleOpenChange(false) which calls onOpenChange(false)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
