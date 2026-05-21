import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { DockerIntegrationPage } from './DockerIntegrationPage'

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

  it('renders empty state for container desktop panel', () => {
    renderWithQueryClient(<DockerIntegrationPage />)
    const desktop = screen.getByTestId('containers-desktop')
    expect(desktop).toBeInTheDocument()
    expect(desktop).toHaveTextContent('No containers discovered yet.')
  })

  it('renders empty state for container mobile panel', () => {
    renderWithQueryClient(<DockerIntegrationPage />)
    const mobile = screen.getByTestId('containers-mobile')
    expect(mobile).toBeInTheDocument()
    expect(mobile).toHaveTextContent('No containers discovered yet.')
  })

  it('renders Pending suggestions section', () => {
    renderWithQueryClient(<DockerIntegrationPage />)
    expect(screen.getByText('Pending suggestions')).toBeInTheDocument()
    expect(screen.getByText('No pending suggestions.')).toBeInTheDocument()
  })

  it('renders Recent actions section', () => {
    renderWithQueryClient(<DockerIntegrationPage />)
    expect(screen.getByText('Recent actions')).toBeInTheDocument()
    expect(screen.getByText('No recent actions.')).toBeInTheDocument()
  })
})
