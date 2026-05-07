import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => vi.fn(),
}))

vi.mock('@/api/queries', () => ({
  useCurrentUser: () => ({ data: { user: { id: 1, username: 'alice' } } }),
  useLogout: () => ({ mutate: vi.fn() }),
}))

import { TooltipProvider } from '@/components/ui/tooltip'
import { TopBar } from './TopBar'

afterEach(() => {
  cleanup()
})

function renderTopBar(
  onToggleSidebar = vi.fn(),
  onToggleMobile = vi.fn(),
  onToggleTheme = vi.fn(),
) {
  return render(
    <TooltipProvider>
      <TopBar
        onToggleSidebar={onToggleSidebar}
        onToggleMobile={onToggleMobile}
        onToggleTheme={onToggleTheme}
      />
    </TooltipProvider>,
  )
}

describe('TopBar', () => {
  it('renders the Toggle sidebar button', () => {
    renderTopBar()
    expect(screen.getByRole('button', { name: 'Toggle sidebar' })).toBeInTheDocument()
  })

  it('renders the disabled Search input', () => {
    renderTopBar()
    expect(screen.getByRole('searchbox', { name: 'Search' })).toBeDisabled()
  })

  it('renders the Notifications button (disabled, coming soon)', () => {
    renderTopBar()
    const btn = screen.getByRole('button', { name: 'Notifications (coming soon)' })
    expect(btn).toBeInTheDocument()
    expect(btn).toBeDisabled()
  })

  it('calls onToggleSidebar when the sidebar button is clicked', async () => {
    const onToggleSidebar = vi.fn()
    renderTopBar(onToggleSidebar)
    await userEvent.click(screen.getByRole('button', { name: 'Toggle sidebar' }))
    expect(onToggleSidebar).toHaveBeenCalledOnce()
  })

  it('renders the user menu button', () => {
    renderTopBar()
    expect(screen.getByRole('button', { name: 'User menu' })).toBeInTheDocument()
  })
})
