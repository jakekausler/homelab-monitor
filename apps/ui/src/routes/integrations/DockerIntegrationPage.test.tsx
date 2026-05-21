import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { DockerIntegrationPage } from './DockerIntegrationPage'

afterEach(() => {
  cleanup()
})

describe('DockerIntegrationPage', () => {
  it('renders the page heading', () => {
    render(<DockerIntegrationPage />)
    expect(screen.getByRole('heading', { name: /docker integration/i })).toBeInTheDocument()
  })

  it('renders empty state for container desktop panel', () => {
    render(<DockerIntegrationPage />)
    const desktop = screen.getByTestId('containers-desktop')
    expect(desktop).toBeInTheDocument()
    expect(desktop).toHaveTextContent('No containers discovered yet.')
  })

  it('renders empty state for container mobile panel', () => {
    render(<DockerIntegrationPage />)
    const mobile = screen.getByTestId('containers-mobile')
    expect(mobile).toBeInTheDocument()
    expect(mobile).toHaveTextContent('No containers discovered yet.')
  })

  it('renders Pending suggestions section', () => {
    render(<DockerIntegrationPage />)
    expect(screen.getByText('Pending suggestions')).toBeInTheDocument()
    expect(screen.getByText('No pending suggestions.')).toBeInTheDocument()
  })

  it('renders Recent actions section', () => {
    render(<DockerIntegrationPage />)
    expect(screen.getByText('Recent actions')).toBeInTheDocument()
    expect(screen.getByText('No recent actions.')).toBeInTheDocument()
  })
})
