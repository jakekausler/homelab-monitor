import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { PullRestartModal } from '@/components/docker/PullRestartModal'

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

const mockMutateAsync = vi.fn()

vi.mock('@/api/docker', () => ({
  useStartPullAndRestart: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
    error: null,
  }),
}))

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

describe('PullRestartModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => cleanup())

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

  it('shows current and latest digest when both are provided', () => {
    render(
      wrap(
        <PullRestartModal
          containerName="caddy"
          open
          onOpenChange={vi.fn()}
          onActionStarted={vi.fn()}
          currentDigest="sha256:aabbcc"
          latestDigest="sha256:ddeeff"
        />,
      ),
    )
    expect(screen.getByText('sha256:aabbcc')).toBeInTheDocument()
    expect(screen.getByText('sha256:ddeeff')).toBeInTheDocument()
  })

  it('shows toast.error with phrase message when mutation rejects with ApiError 400', async () => {
    mockMutateAsync.mockRejectedValueOnce(
      new ApiError({
        status: 400,
        code: 'BAD_REQUEST',
        message: 'Bad Request',
        retryAfterSeconds: null,
        details: null,
      }),
    )
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Confirm phrase must be "pull"')
    })
  })

  it('shows toast.error with container-not-found message when mutation rejects with ApiError 404', async () => {
    mockMutateAsync.mockRejectedValueOnce(
      new ApiError({
        status: 404,
        code: 'NOT_FOUND',
        message: 'Not Found',
        retryAfterSeconds: null,
        details: null,
      }),
    )
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Container not found: caddy')
    })
  })

  it('shows toast.error with forbidden message when mutation rejects with ApiError 403', async () => {
    mockMutateAsync.mockRejectedValueOnce(
      new ApiError({
        status: 403,
        code: 'FORBIDDEN',
        message: 'Forbidden',
        retryAfterSeconds: null,
        details: null,
      }),
    )
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Forbidden — your session lacks docker:write')
    })
  })

  it('shows toast.info with already-in-progress message when mutation rejects with ApiError 409', async () => {
    mockMutateAsync.mockRejectedValueOnce(
      new ApiError({
        status: 409,
        code: 'CONFLICT',
        message: 'Conflict',
        retryAfterSeconds: null,
        details: null,
      }),
    )
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.info).toHaveBeenCalledWith('Pull & Restart already in progress for caddy')
    })
  })

  it('shows generic ApiError message for unhandled status codes', async () => {
    mockMutateAsync.mockRejectedValueOnce(
      new ApiError({
        status: 500,
        code: 'INTERNAL_ERROR',
        message: 'Server exploded',
        retryAfterSeconds: null,
        details: null,
      }),
    )
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Server exploded')
    })
  })

  it('shows fallback error message when non-ApiError is thrown', async () => {
    mockMutateAsync.mockRejectedValueOnce(new Error('network timeout'))
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
    const input = screen.getByLabelText(/type pull to confirm/i)
    fireEvent.change(input, { target: { value: 'pull' } })
    fireEvent.click(screen.getByRole('button', { name: /pull & restart/i }))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Pull & Restart failed')
    })
  })
})
