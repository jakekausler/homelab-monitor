import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { ContainerPage } from '../ContainerPage'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useParams: () => ({ name: 'test-container' }),
    useLocation: () => ({ pathname: '/integrations/docker/containers/test-container/overview' }),
    Outlet: () => <div>Outlet content</div>,
    Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
      <a href={typeof to === 'string' ? to : '#'}>{children}</a>
    ),
  }
})

vi.mock('@/api/docker', () => ({
  useListContainers: () => ({
    data: {
      containers: [
        {
          name: 'test-container',
          image: 'test-image:latest',
          status: 'running',
        },
      ],
    },
  }),
}))

afterEach(cleanup)

const TestWrapper = ({ children }: { children: React.ReactNode }) => {
  const queryClient = new QueryClient()
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('ContainerPage', () => {
  it('renders container name in header', () => {
    render(
      <TestWrapper>
        <ContainerPage />
      </TestWrapper>,
    )
    expect(screen.getByText('test-container')).toBeInTheDocument()
  })

  it('renders back link to docker integration', () => {
    render(
      <TestWrapper>
        <ContainerPage />
      </TestWrapper>,
    )
    expect(screen.getByText('Back to Docker integration')).toBeInTheDocument()
  })

  it('renders tab navigation', () => {
    render(
      <TestWrapper>
        <ContainerPage />
      </TestWrapper>,
    )
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })

  it('renders Outlet content', () => {
    render(
      <TestWrapper>
        <ContainerPage />
      </TestWrapper>,
    )
    expect(screen.getByText('Outlet content')).toBeInTheDocument()
  })
})
