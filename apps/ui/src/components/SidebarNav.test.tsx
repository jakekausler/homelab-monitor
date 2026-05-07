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

  it('renders disabled nav items for coming-soon features', () => {
    renderNav()
    // Alerts, Inventory etc are rendered as disabled buttons
    const alertsBtn = screen.getByRole('button', { name: /Alerts/ })
    expect(alertsBtn).toBeDisabled()
  })

  it('hides item labels when collapsed', () => {
    renderNav(true)
    // Labels are inside <span> elements that are omitted when collapsed
    expect(screen.queryByText('Alerts')).not.toBeInTheDocument()
    expect(screen.queryByText('Overview')).not.toBeInTheDocument()
  })
})
