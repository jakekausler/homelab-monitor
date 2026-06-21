import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { NetworkLayout } from './NetworkLayout'

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
    Outlet: () => <div data-testid="network-outlet" />,
  }
})

afterEach(() => cleanup())

describe('NetworkLayout', () => {
  it('renders header, both tabs, and outlet', () => {
    render(<NetworkLayout />)
    expect(screen.getByRole('heading', { name: /^network$/i })).toBeInTheDocument()
    expect(screen.getByTestId('network-tab-overview')).toHaveTextContent('Overview')
    expect(screen.getByTestId('network-tab-clients')).toHaveTextContent('Clients')
    expect(screen.getByTestId('network-outlet')).toBeInTheDocument()
  })
})
