import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { LogsLayout } from '../LogsLayout'

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
    Outlet: () => <div data-testid="logs-outlet" />,
  }
})

afterEach(() => {
  cleanup()
})

describe('LogsLayout', () => {
  it('renders both tab links with correct labels', () => {
    render(<LogsLayout />)
    expect(screen.getByText('Query')).toBeInTheDocument()
    expect(screen.getByText('Signatures')).toBeInTheDocument()
    expect(screen.getByText('Models')).toBeInTheDocument()
  })

  it('renders tab links with correct data-testid attributes', () => {
    render(<LogsLayout />)
    expect(screen.getByTestId('logs-tab-query')).toBeInTheDocument()
    expect(screen.getByTestId('logs-tab-signatures')).toBeInTheDocument()
    expect(screen.getByTestId('logs-tab-models-debug')).toBeInTheDocument()
  })

  it('renders the tabs nav with correct aria-label', () => {
    render(<LogsLayout />)
    expect(screen.getByRole('navigation', { name: 'Logs tabs' })).toBeInTheDocument()
  })

  it('renders an Outlet host', () => {
    render(<LogsLayout />)
    expect(screen.getByTestId('logs-outlet')).toBeInTheDocument()
  })
})
