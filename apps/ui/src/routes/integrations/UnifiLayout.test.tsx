// SCAFFOLD TEST: validates honest empty-state placeholder only; real behavior tests arrive in STAGE-020/021
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { UnifiLayout } from './UnifiLayout'

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
    Outlet: () => <div data-testid="unifi-outlet" />,
  }
})

afterEach(() => {
  cleanup()
})

describe('UnifiLayout', () => {
  it('renders without crashing', () => {
    render(<UnifiLayout />)
  })

  it('renders the page heading', () => {
    render(<UnifiLayout />)
    expect(screen.getByRole('heading', { name: /unifi integration/i })).toBeInTheDocument()
  })

  it('renders the honest placeholder subtitle describing upcoming stages', () => {
    render(<UnifiLayout />)
    expect(
      screen.getByText('Unifi gear, network, and clients land in upcoming stages.'),
    ).toBeInTheDocument()
  })

  it('renders the Overview tab link', () => {
    render(<UnifiLayout />)
    expect(screen.getByTestId('unifi-tab-overview')).toBeInTheDocument()
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })

  it('renders the tabs nav with correct aria-label', () => {
    render(<UnifiLayout />)
    expect(screen.getByRole('navigation', { name: 'Unifi tabs' })).toBeInTheDocument()
  })

  it('renders an Outlet host', () => {
    render(<UnifiLayout />)
    expect(screen.getByTestId('unifi-outlet')).toBeInTheDocument()
  })

  it('does not render any fake device/network/client data tables', () => {
    render(<UnifiLayout />)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  })
})
