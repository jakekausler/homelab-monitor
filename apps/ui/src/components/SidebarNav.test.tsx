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

  it('shows "homelab-monitor" brand text when expanded', () => {
    renderNav(false)
    expect(screen.getByText('homelab-monitor')).toBeInTheDocument()
  })

  it('hides the brand text when collapsed', () => {
    renderNav(true)
    expect(screen.queryByText('homelab-monitor')).not.toBeInTheDocument()
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

  it('renders Inventory as enabled link (STAGE-001-021)', () => {
    renderNav()
    // Inventory is now enabled as a link
    const inventoryLink = screen.getByRole('link', { name: /Inventory/ })
    expect(inventoryLink).toBeInTheDocument()
  })

  it('hides item labels when collapsed', () => {
    renderNav(true)
    // Labels are inside <span> elements that are omitted when collapsed
    expect(screen.queryByText('Alerts')).not.toBeInTheDocument()
    expect(screen.queryByText('Overview')).not.toBeInTheDocument()
    expect(screen.queryByText('Metrics')).not.toBeInTheDocument()
  })
})
