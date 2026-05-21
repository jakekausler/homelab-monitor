import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ContainerLogsPlaceholderPage } from './ContainerLogsPlaceholder'

vi.mock('@tanstack/react-router', () => ({
  Link: ({
    children,
    to,
    className,
  }: {
    children: React.ReactNode
    to: string
    className?: string
  }) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
  useParams: () => ({ name: 'test-container' }),
}))

afterEach(() => {
  cleanup()
})

describe('ContainerLogsPlaceholderPage', () => {
  it('renders the placeholder copy with the container name interpolated', () => {
    render(<ContainerLogsPlaceholderPage />)
    expect(screen.getByText(/Log viewer for/i)).toBeInTheDocument()
    expect(screen.getByText('test-container')).toBeInTheDocument()
    expect(screen.getByText(/not yet implemented/i)).toBeInTheDocument()
  })

  it('renders a back-link to /integrations/docker', () => {
    render(<ContainerLogsPlaceholderPage />)
    const link = screen.getByRole('link', { name: /back to docker integration/i })
    expect(link).toBeInTheDocument()
    expect(link.getAttribute('href')).toBe('/integrations/docker')
  })
})
