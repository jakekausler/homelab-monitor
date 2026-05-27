import { afterEach, describe, it, expect, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { ContainerTabsNav } from '../ContainerTabsNav'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    useLocation: () => ({ pathname: '/integrations/docker/containers/test/overview' }),
    Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
      <a href={to}>{children}</a>
    ),
  }
})

afterEach(cleanup)

describe('ContainerTabsNav', () => {
  it('renders all four tabs', () => {
    render(<ContainerTabsNav name="test-container" />)
    expect(screen.getByText('Overview')).toBeInTheDocument()
    expect(screen.getByText('Probes')).toBeInTheDocument()
    expect(screen.getByText('Logs')).toBeInTheDocument()
    expect(screen.getByText('Recent Actions')).toBeInTheDocument()
  })

  it('renders tabs as links', () => {
    render(<ContainerTabsNav name="test-container" />)
    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(4)
  })
})
