import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Project test conventions:
// - Framework: Vitest with vi.mock()
// - Async: async/await + userEvent
// - Mocking: vi.mock() at top, then vi.mocked() for typed access

vi.mock('@tanstack/react-router', () => ({
  useNavigate: () => vi.fn(),
}))

vi.mock('@/api/queries', () => ({
  useCurrentUser: vi.fn(),
  useLogout: vi.fn(),
}))

import { useCurrentUser, useLogout } from '@/api/queries'
import { TooltipProvider } from '@/components/ui/tooltip'
import { UserMenu } from './UserMenu'

const mockCurrentUser = vi.mocked(useCurrentUser)
const mockLogout = vi.mocked(useLogout)

afterEach(() => {
  cleanup()
})

function renderMenu(onToggleTheme = vi.fn()) {
  return render(
    <TooltipProvider>
      <UserMenu onToggleTheme={onToggleTheme} />
    </TooltipProvider>,
  )
}

describe('UserMenu', () => {
  it('renders the username from useCurrentUser', () => {
    mockCurrentUser.mockReturnValue({
      data: { user: { id: 1, username: 'alice' } },
    } as unknown as ReturnType<typeof useCurrentUser>)
    mockLogout.mockReturnValue({ mutate: vi.fn() } as unknown as ReturnType<typeof useLogout>)

    renderMenu()
    expect(screen.getByRole('button', { name: 'User menu' })).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
  })

  it('falls back to "unknown" when data is undefined', () => {
    mockCurrentUser.mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof useCurrentUser>)
    mockLogout.mockReturnValue({ mutate: vi.fn() } as unknown as ReturnType<typeof useLogout>)

    renderMenu()
    expect(screen.getByText('unknown')).toBeInTheDocument()
  })

  it('calls logout.mutate when Sign out is clicked (lines 45-47)', async () => {
    const mutate = vi.fn()
    mockCurrentUser.mockReturnValue({
      data: { user: { id: 1, username: 'alice' } },
    } as unknown as ReturnType<typeof useCurrentUser>)
    mockLogout.mockReturnValue({ mutate } as unknown as ReturnType<typeof useLogout>)

    renderMenu()
    // Open the dropdown
    await userEvent.click(screen.getByRole('button', { name: 'User menu' }))
    // Click Sign out
    await userEvent.click(screen.getByText('Sign out'))
    expect(mutate).toHaveBeenCalledWith(
      undefined,
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- expect.any returns AsymmetricMatcher typed as any
      expect.objectContaining({ onSettled: expect.any(Function) }),
    )
  })

  it('calls onToggleTheme when Toggle theme is clicked', async () => {
    const onToggleTheme = vi.fn()
    mockCurrentUser.mockReturnValue({
      data: { user: { id: 1, username: 'alice' } },
    } as unknown as ReturnType<typeof useCurrentUser>)
    mockLogout.mockReturnValue({ mutate: vi.fn() } as unknown as ReturnType<typeof useLogout>)

    renderMenu(onToggleTheme)
    await userEvent.click(screen.getByRole('button', { name: 'User menu' }))
    await userEvent.click(screen.getByText('Toggle theme'))
    expect(onToggleTheme).toHaveBeenCalledOnce()
  })
})
