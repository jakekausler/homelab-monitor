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
