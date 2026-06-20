import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@tanstack/react-router', () => ({
  Link: ({
    children,
    to,
    ...rest
  }: {
    children: React.ReactNode
    to?: string
    [k: string]: unknown
  }) => (
    <a href={to} {...rest}>
      {children}
    </a>
  ),
}))

import { TooltipProvider } from '@/components/ui/tooltip'
import { SidebarNav } from './SidebarNav'

afterEach(() => {
  cleanup()
})

function renderNav(collapsed = false) {
  return render(
    <TooltipProvider>
      <SidebarNav collapsed={collapsed} />
    </TooltipProvider>,
  )
}

describe('SidebarNav', () => {
  it('renders the primary nav landmark', () => {
    renderNav()
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
  })

  it('shows "Homelab Monitor" brand text when expanded', () => {
    renderNav(false)
    expect(screen.getByText('Homelab Monitor')).toBeInTheDocument()
  })

  it('hides the brand text when collapsed', () => {
    renderNav(true)
    expect(screen.queryByText('Homelab Monitor')).not.toBeInTheDocument()
  })

  it('renders the Overview link', () => {
    renderNav()
    expect(screen.getByRole('link', { name: /Overview/ })).toBeInTheDocument()
  })

  it('renders the Alerts link as enabled (STAGE-001-019)', () => {
    renderNav()
    const alertsLink = screen.getByRole('link', { name: /Alerts/ })
    expect(alertsLink).toBeInTheDocument()
    expect(alertsLink.getAttribute('href')).toBe('/alerts')
  })

  it('renders the Metrics link as enabled (STAGE-001-020)', () => {
    renderNav()
    const metricsLink = screen.getByRole('link', { name: /Metrics/ })
    expect(metricsLink).toBeInTheDocument()
    expect(metricsLink.getAttribute('href')).toBe('/metrics')
  })

  it('renders Crons under Integrations as an enabled link (STAGE-007-018)', () => {
    renderNav()
    const cronsLink = screen.getByRole('link', { name: /Crons/ })
    expect(cronsLink.getAttribute('href')).toBe('/integrations/crons')
  })

  it('renders Network and Unifi placeholder links (STAGE-007-018)', () => {
    renderNav()
    expect(screen.getByRole('link', { name: /Network/ }).getAttribute('href')).toBe(
      '/integrations/network',
    )
    expect(screen.getByRole('link', { name: /Unifi/ }).getAttribute('href')).toBe(
      '/integrations/unifi',
    )
  })

  it('hides item labels when collapsed', () => {
    renderNav(true)
    // Labels are inside <span> elements that are omitted when collapsed
    expect(screen.queryByText('Alerts')).not.toBeInTheDocument()
    expect(screen.queryByText('Overview')).not.toBeInTheDocument()
    expect(screen.queryByText('Metrics')).not.toBeInTheDocument()
  })
})
