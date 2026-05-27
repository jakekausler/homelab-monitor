import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { DockerIntegrationPage } from './DockerIntegrationPage'

vi.mock('@/api/docker', () => ({
  useListContainers: () => ({
    data: { containers: [] },
    isLoading: false,
    isError: false,
  }),
  useListDockerSuggestions: () => ({
    data: { pages: [{ suggestions: [], next_cursor: null }], pageParams: [undefined] },
    hasNextPage: false,
    isFetchingNextPage: false,
    isLoading: false,
    isError: false,
    fetchNextPage: vi.fn(),
  }),
  useProbesSummary: () => ({
    data: {},
    isPending: false,
  }),
  useImageUpdatesSummary: () => ({ data: null }),
  useListComposeActions: () => ({ data: { actions: [] }, isPending: false, isError: false }),
  dockerQueryKeys: {},
  DockerSuggestionStatus: {},
}))

afterEach(() => {
  cleanup()
})

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('DockerIntegrationPage', () => {
  it('renders the page heading', () => {
    renderWithQueryClient(<DockerIntegrationPage />)
    expect(screen.getByRole('heading', { name: /docker integration/i })).toBeInTheDocument()
  })
})
