import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import { MetricsLayout } from './MetricsLayout'

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
    Outlet: () => <div data-testid="metrics-outlet" />,
  }
})

afterEach(() => {
  cleanup()
})

describe('MetricsLayout', () => {
  it('renders all tab links with correct labels', () => {
    render(<MetricsLayout />)
    expect(screen.getByText('System')).toBeInTheDocument()
    expect(screen.getByText('Containers')).toBeInTheDocument()
    expect(screen.getByText('Collectors')).toBeInTheDocument()
    expect(screen.getByText('Heartbeats')).toBeInTheDocument()
    expect(screen.getByText('Storage & Logs')).toBeInTheDocument()
    expect(screen.getByText('Home Assistant')).toBeInTheDocument()
  })

  it('renders tab links pointing at the route paths', () => {
    render(<MetricsLayout />)
    expect(screen.getByTestId('metrics-tab-system').getAttribute('href')).toBe('/metrics/system')
    expect(screen.getByTestId('metrics-tab-containers').getAttribute('href')).toBe(
      '/metrics/containers',
    )
    expect(screen.getByTestId('metrics-tab-collectors').getAttribute('href')).toBe(
      '/metrics/collectors',
    )
    expect(screen.getByTestId('metrics-tab-heartbeats').getAttribute('href')).toBe(
      '/metrics/heartbeats',
    )
    expect(screen.getByTestId('metrics-tab-storage-logs').getAttribute('href')).toBe(
      '/metrics/storage-logs',
    )
    expect(screen.getByTestId('metrics-tab-home-assistant').getAttribute('href')).toBe(
      '/metrics/home-assistant',
    )
  })

  it('renders the tabs nav with correct aria-label', () => {
    render(<MetricsLayout />)
    expect(screen.getByRole('navigation', { name: 'Metrics tabs' })).toBeInTheDocument()
  })

  it('renders the page heading', () => {
    render(<MetricsLayout />)
    expect(screen.getByRole('heading', { name: /metrics/i })).toBeInTheDocument()
  })

  it('renders an Outlet host', () => {
    render(<MetricsLayout />)
    expect(screen.getByTestId('metrics-outlet')).toBeInTheDocument()
  })
})
