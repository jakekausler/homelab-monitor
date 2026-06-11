import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { AlertsLayout } from '../AlertsLayout'

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
    Outlet: () => <div data-testid="alerts-outlet" />,
  }
})

afterEach(() => {
  cleanup()
})

describe('AlertsLayout', () => {
  it('renders both tab links with correct labels', () => {
    render(<AlertsLayout />)
    expect(screen.getByText('Active Alerts')).toBeInTheDocument()
    expect(screen.getByText('Manage Rules')).toBeInTheDocument()
  })

  it('renders tab links with correct data-testid attributes', () => {
    render(<AlertsLayout />)
    expect(screen.getByTestId('alerts-tab-active')).toBeInTheDocument()
    expect(screen.getByTestId('alerts-tab-manage')).toBeInTheDocument()
  })

  it('renders the tabs nav with correct aria-label', () => {
    render(<AlertsLayout />)
    expect(screen.getByRole('navigation', { name: 'Alerts tabs' })).toBeInTheDocument()
  })

  it('renders an Outlet host', () => {
    render(<AlertsLayout />)
    expect(screen.getByTestId('alerts-outlet')).toBeInTheDocument()
  })
})
