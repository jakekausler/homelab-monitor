import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ProbesPanel } from '../ProbesPanel'
import {
  useListContainers,
  useListProbes,
  useListDockerSuggestions,
  useSuggestionDefaultProbes,
  useCreateProbeTarget,
  useUpdateProbeTarget,
  useDeleteProbeTarget,
} from '@/api/docker'

vi.mock('@/api/docker', () => ({
  useListContainers: vi.fn(),
  useListProbes: vi.fn(),
  useListDockerSuggestions: vi.fn(),
  useSuggestionDefaultProbes: vi.fn(),
  useCreateProbeTarget: vi.fn(),
  useUpdateProbeTarget: vi.fn(),
  useDeleteProbeTarget: vi.fn(),
}))

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ProbesPanel', () => {
  beforeEach(() => {
    vi.mocked(useListProbes).mockReturnValue({
      data: { probes: [] },
      isLoading: false,
      isError: false,
    } as never)
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: { pages: [{ suggestions: [] }] },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: undefined,
      isLoading: false,
    } as never)
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
  })

  afterEach(() => {
    cleanup()
  })

  it('renders one card per container', () => {
    vi.mocked(useListContainers).mockReturnValue({
      data: {
        containers: [
          { id: 'c1', name: 'web', image: 'nginx', status: 'running', labels: {} },
          { id: 'c2', name: 'db', image: 'postgres', status: 'running', labels: {} },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    const { container: dom } = renderWithClient(<ProbesPanel />)
    const cards = dom.querySelectorAll('[data-testid="container-probes-card"]')
    expect(cards).toHaveLength(2)
  })

  it('renders loading state', () => {
    vi.mocked(useListContainers).mockReturnValue({
      isLoading: true,
      isError: false,
      data: undefined,
    } as never)
    renderWithClient(<ProbesPanel />)
    expect(screen.getByTestId('probes-loading')).toBeInTheDocument()
  })

  it('renders error state', () => {
    vi.mocked(useListContainers).mockReturnValue({
      isLoading: false,
      isError: true,
      data: undefined,
    } as never)
    renderWithClient(<ProbesPanel />)
    expect(screen.getByTestId('probes-error')).toBeInTheDocument()
  })

  it('renders empty state when no containers', () => {
    vi.mocked(useListContainers).mockReturnValue({
      isLoading: false,
      isError: false,
      data: { containers: [] },
    } as never)
    renderWithClient(<ProbesPanel />)
    expect(screen.getByTestId('probes-empty')).toBeInTheDocument()
  })
})
