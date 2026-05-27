import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { ContainerProbesTab } from '../ContainerProbesTab'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useParams: () => ({ name: 'test-container' }),
  }
})

vi.mock('../ContainerProbesCard', () => ({
  ContainerProbesCard: () => <div>Probes card</div>,
}))

const mockUseListContainers = vi.fn(() => ({
  data: {
    containers: [
      {
        id: 'id-1',
        name: 'test-container',
        status: 'running',
        image: 'test-image:latest',
        labels: {},
      },
    ],
  },
  isPending: false,
}))

vi.mock('@/api/docker', () => ({
  useListContainers: () => mockUseListContainers(),
}))

const TestWrapper = ({ children }: { children: React.ReactNode }) => {
  const queryClient = new QueryClient()
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('ContainerProbesTab', () => {
  afterEach(() => cleanup())

  it('renders probes card when container is found', () => {
    render(
      <TestWrapper>
        <ContainerProbesTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Probes card')).toBeInTheDocument()
  })

  it('shows loading message when containers are pending and container not yet found', () => {
    mockUseListContainers.mockReturnValueOnce({
      data: undefined,
      isPending: true,
    } as never)
    render(
      <TestWrapper>
        <ContainerProbesTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows container not found message when container is absent from list', () => {
    mockUseListContainers.mockReturnValueOnce({
      data: {
        containers: [
          { id: 'other', name: 'other-container', status: 'running', image: 'img', labels: {} },
        ],
      },
      isPending: false,
    })
    render(
      <TestWrapper>
        <ContainerProbesTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Container not found.')).toBeInTheDocument()
  })
})
