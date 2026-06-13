import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { HomeAssistantLayout } from './HomeAssistantLayout'

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual('@tanstack/react-router')
  return {
    ...actual,
    Link: ({
      children,
      to,
      ...rest
    }: {
      children: ReactNode
      to: string
      'data-testid'?: string
    }) => (
      <a href={to} data-testid={rest['data-testid']}>
        {children}
      </a>
    ),
    Outlet: () => <div data-testid="home-assistant-outlet" />,
  }
})

afterEach(() => {
  cleanup()
})

describe('HomeAssistantLayout', () => {
  it('renders all three tab links with correct labels', () => {
    render(<HomeAssistantLayout />)
    expect(screen.getByText('Health')).toBeInTheDocument()
    expect(screen.getByText('Status')).toBeInTheDocument()
    expect(screen.getByText('Logs')).toBeInTheDocument()
  })

  it('renders tab links with correct data-testid attributes', () => {
    render(<HomeAssistantLayout />)
    expect(screen.getByTestId('home-assistant-tab-health')).toBeInTheDocument()
    expect(screen.getByTestId('home-assistant-tab-status')).toBeInTheDocument()
    expect(screen.getByTestId('home-assistant-tab-logs')).toBeInTheDocument()
  })

  it('renders the tabs nav with correct aria-label', () => {
    render(<HomeAssistantLayout />)
    expect(screen.getByRole('navigation', { name: 'Home Assistant tabs' })).toBeInTheDocument()
  })

  it('renders the page heading', () => {
    render(<HomeAssistantLayout />)
    expect(screen.getByRole('heading', { name: /home assistant integration/i })).toBeInTheDocument()
  })

  it('renders an Outlet host', () => {
    render(<HomeAssistantLayout />)
    expect(screen.getByTestId('home-assistant-outlet')).toBeInTheDocument()
  })
})
