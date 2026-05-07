import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@tanstack/react-router', () => ({
  Outlet: () => <div data-testid="outlet" />,
  Link: ({ children, ...rest }: { children: React.ReactNode; [k: string]: unknown }) => (
    <a {...rest}>{children}</a>
  ),
  useNavigate: () => vi.fn(),
}))

vi.mock('@/api/queries', () => ({
  useCurrentUser: () => ({
    data: { user: { id: 1, username: 'alice' } },
  }),
  useLogout: () => ({ mutate: vi.fn() }),
}))

import { TooltipProvider } from '@/components/ui/tooltip'
import { AppShell } from './AppShell'

afterEach(() => {
  cleanup()
})

function renderShell() {
  return render(
    <TooltipProvider>
      <AppShell />
    </TooltipProvider>,
  )
}

describe('AppShell', () => {
  it('renders the sidebar items and the outlet', () => {
    renderShell()
    expect(screen.getByText('Overview')).toBeInTheDocument()
    expect(screen.getByText('Alerts')).toBeInTheDocument()
    expect(screen.getByText('Settings')).toBeInTheDocument()
    expect(screen.getByTestId('outlet')).toBeInTheDocument()
  })

  it('collapses the sidebar when the Toggle sidebar button is clicked', async () => {
    renderShell()
    // Sidebar starts expanded — brand text visible
    expect(screen.getByText('homelab-monitor')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Toggle sidebar' }))
    expect(screen.queryByText('homelab-monitor')).not.toBeInTheDocument()
  })

  it('opens the mobile sidebar when "Open navigation menu" is clicked', async () => {
    renderShell()
    expect(screen.queryByRole('dialog', { name: 'Navigation menu' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    expect(screen.getByRole('dialog', { name: 'Navigation menu' })).toBeInTheDocument()
  })

  it('closes the mobile sidebar when the Close menu button is clicked', async () => {
    renderShell()
    await userEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    expect(screen.getByRole('dialog', { name: 'Navigation menu' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Close menu' }))
    expect(screen.queryByRole('dialog', { name: 'Navigation menu' })).not.toBeInTheDocument()
  })

  it('closes the mobile sidebar when Esc is pressed', async () => {
    renderShell()
    await userEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    expect(screen.getByRole('dialog', { name: 'Navigation menu' })).toBeInTheDocument()
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(screen.queryByRole('dialog', { name: 'Navigation menu' })).not.toBeInTheDocument()
  })

  it('toggles the theme data-attribute on the document root', async () => {
    renderShell()
    // Initial theme is 'dark' (default when localStorage is empty)
    expect(document.documentElement.dataset.theme).toBe('dark')

    // Open the UserMenu dropdown and click Toggle theme
    await userEvent.click(screen.getByRole('button', { name: 'User menu' }))
    await act(async () => {
      await userEvent.click(screen.getByText('Toggle theme'))
    })
    expect(document.documentElement.dataset.theme).toBe('light')
  })
})
