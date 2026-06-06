import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/annotations', () => ({
  useSignatureAnnotations: vi.fn(),
  useAddAnnotation: vi.fn(),
  useDeleteAnnotation: vi.fn(),
}))

import { useSignatureAnnotations, useAddAnnotation, useDeleteAnnotation } from '@/api/annotations'
import { SignatureAnnotations } from '../SignatureAnnotations'

const mockList = vi.mocked(useSignatureAnnotations)
const mockAdd = vi.mocked(useAddAnnotation)
const mockDelete = vi.mocked(useDeleteAnnotation)

afterEach(cleanup)

function setMocks(
  annotations: Array<{ id: number; author: string; created_at: string; note: string }>,
  addMutate = vi.fn(),
  deleteMutate = vi.fn(),
  addPending = false,
  deletePending = false,
) {
  mockList.mockReturnValue({
    data: { annotations },
  } as unknown as ReturnType<typeof useSignatureAnnotations>)
  mockAdd.mockReturnValue({
    isPending: addPending,
    mutate: addMutate,
  } as unknown as ReturnType<typeof useAddAnnotation>)
  mockDelete.mockReturnValue({
    isPending: deletePending,
    mutate: deleteMutate,
  } as unknown as ReturnType<typeof useDeleteAnnotation>)
}

describe('SignatureAnnotations', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty state', () => {
    setMocks([])
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    expect(screen.getByText('No annotations yet.')).toBeInTheDocument()
  })

  it('renders annotation items', () => {
    setMocks([
      {
        id: 1,
        author: 'me',
        created_at: '2026-01-01T00:00:00+00:00',
        note: 'hello',
      },
    ])
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    expect(screen.getByTestId('annotation-item')).toBeInTheDocument()
    expect(screen.getByText('hello')).toBeInTheDocument()
    expect(screen.getByText(/me/)).toBeInTheDocument()
  })

  it('add button disabled when empty', () => {
    setMocks([])
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const button = screen.getByTestId('annotation-add-button')
    expect(button).toBeDisabled()
  })

  it('add button enabled after typing, fires mutate with note', () => {
    const addMutate = vi.fn()
    setMocks([], addMutate)
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const input = screen.getByTestId('annotation-input')
    const button = screen.getByTestId('annotation-add-button')
    fireEvent.change(input, { target: { value: 'new note' } })
    expect(button).not.toBeDisabled()
    fireEvent.click(button)
    expect(addMutate).toHaveBeenCalledWith(
      expect.objectContaining({ body: { note: 'new note' } }),
      expect.any(Object),
    )
  })

  it('add button stays disabled for whitespace-only', () => {
    setMocks([])
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const input = screen.getByTestId('annotation-input')
    const button = screen.getByTestId('annotation-add-button')
    fireEvent.change(input, { target: { value: '   ' } })
    expect(button).toBeDisabled()
  })

  it('delete fires confirm + mutate', () => {
    const deleteMutate = vi.fn()
    setMocks(
      [
        {
          id: 7,
          author: 'me',
          created_at: '2026-01-01T00:00:00+00:00',
          note: 'test',
        },
      ],
      vi.fn(),
      deleteMutate,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const deleteButton = screen.getByRole('button', { name: /Delete annotation/i })
    fireEvent.click(deleteButton)
    expect(deleteMutate).toHaveBeenCalledWith(expect.objectContaining({ annotationId: 7 }))
    vi.restoreAllMocks()
  })

  it('delete cancelled when confirm false', () => {
    const deleteMutate = vi.fn()
    setMocks(
      [
        {
          id: 7,
          author: 'me',
          created_at: '2026-01-01T00:00:00+00:00',
          note: 'test',
        },
      ],
      vi.fn(),
      deleteMutate,
    )
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const deleteButton = screen.getByRole('button', { name: /Delete annotation/i })
    fireEvent.click(deleteButton)
    expect(deleteMutate).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })

  it('add button disabled while pending', () => {
    setMocks([], vi.fn(), vi.fn(), true)
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    const input = screen.getByTestId('annotation-input')
    const button = screen.getByTestId('annotation-add-button')
    fireEvent.change(input, { target: { value: 'test' } })
    expect(button).toBeDisabled()
  })

  it('delete button disabled while a delete is pending', () => {
    setMocks(
      [
        {
          id: 7,
          author: 'me',
          created_at: '2026-01-01T00:00:00+00:00',
          note: 'test',
        },
      ],
      vi.fn(),
      vi.fn(),
      false,
      true,
    )
    render(<SignatureAnnotations templateHash="h" serviceKey="s" />)
    expect(screen.getByRole('button', { name: /Delete annotation/i })).toBeDisabled()
  })
})
