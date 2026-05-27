import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClientProvider, QueryClient } from '@tanstack/react-query'
import { ContainerActionsTab } from '../ContainerActionsTab'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useParams: () => ({ name: 'test-container' }),
  }
})

vi.mock('../RecentActionsPanel', () => ({
  RecentActionsPanel: () => <div>Recent actions panel</div>,
}))

const TestWrapper = ({ children }: { children: React.ReactNode }) => {
  const queryClient = new QueryClient()
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('ContainerActionsTab', () => {
  it('renders recent actions panel', () => {
    render(
      <TestWrapper>
        <ContainerActionsTab />
      </TestWrapper>,
    )
    expect(screen.getByText('Recent actions panel')).toBeInTheDocument()
  })
})
