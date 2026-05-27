import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { ContainerLogsTab } from '../ContainerLogsTab'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useParams: () => ({ name: 'test-container' }),
  }
})

vi.mock('../DockerContainerLogsViewerBody', () => ({
  DockerContainerLogsViewerBody: () => <div>Logs viewer body</div>,
}))

const TestWrapper = ({ children }: { children: React.ReactNode }) => {
  const queryClient = new QueryClient()
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('ContainerLogsTab', () => {
  it('renders logs viewer body', () => {
    render(
      <TestWrapper>
        <ContainerLogsTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Logs viewer body')).toBeInTheDocument()
  })
})
