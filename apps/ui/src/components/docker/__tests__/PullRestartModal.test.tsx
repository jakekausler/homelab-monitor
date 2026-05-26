import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PullRestartModal } from '@/components/docker/PullRestartModal'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('@/api/docker', () => ({
  useStartPullAndRestart: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
    error: null,
  }),
}))

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

describe('PullRestartModal', () => {
  it('disables the submit button until phrase matches "pull"', () => {
    const onOpenChange = vi.fn()
    const onActionStarted = vi.fn()
    render(
      wrap(
        <PullRestartModal
          containerName="caddy"
          open
          onOpenChange={onOpenChange}
          onActionStarted={onActionStarted}
        />,
      ),
    )
    const submit = screen.getByRole('button', { name: /pull & restart/i })
    expect(submit).toBeDisabled()
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'wrong' } })
    expect(submit).toBeDisabled()
    fireEvent.change(input, { target: { value: 'pull' } })
    expect(submit).not.toBeDisabled()
  })

  it('renders modal content when open', () => {
    render(
      wrap(
        <PullRestartModal
          containerName="caddy"
          open
          onOpenChange={vi.fn()}
          onActionStarted={vi.fn()}
        />,
      ),
    )
    expect(screen.getByRole('heading', { name: /pull & restart/i })).toBeInTheDocument()
    expect(screen.getByLabelText(/type pull to confirm/i)).toBeInTheDocument()
  })

  it('shows "Pull & Restart" label by default', () => {
    render(
      wrap(
        <PullRestartModal
          containerName="myapp"
          open={true}
          onOpenChange={vi.fn()}
          onActionStarted={vi.fn()}
        />,
      ),
    )
    expect(screen.getByRole('button', { name: /Pull & Restart/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Pull & Restart myapp/i })).toBeInTheDocument()
  })

  it('shows "Rebuild & Restart" label when actionLabel prop is set', () => {
    render(
      wrap(
        <PullRestartModal
          containerName="myapp"
          open={true}
          onOpenChange={vi.fn()}
          onActionStarted={vi.fn()}
          actionLabel="Rebuild & Restart"
          confirmPhrase="rebuild"
        />,
      ),
    )
    expect(screen.getByRole('button', { name: /Rebuild & Restart/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Rebuild & Restart myapp/i })).toBeInTheDocument()
    expect(screen.getByText(/rebuild/i, { selector: 'strong' })).toBeInTheDocument()
  })
})
