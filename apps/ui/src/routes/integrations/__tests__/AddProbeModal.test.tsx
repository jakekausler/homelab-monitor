import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { AddProbeModal } from '../AddProbeModal'
import { useCreateProbeTarget } from '@/api/docker'
import { ApiError } from '@/api/client'
import { toast } from 'sonner'

vi.mock('@/api/docker', () => ({
  useCreateProbeTarget: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('AddProbeModal', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
  })

  it('renders modal when open', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('add-probe-modal')).toBeInTheDocument()
  })

  it('sets kind to http by default', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('add-probe-kind')).toHaveValue('http')
  })

  it('disables submit when target is empty', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('add-probe-submit')).toBeDisabled()
    expect(screen.getByText('Target is required')).toBeInTheDocument()
  })

  it('calls mutateAsync on valid submit', () => {
    const mutate = vi.fn().mockResolvedValue(null)
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)

    fireEvent.change(screen.getByTestId('add-probe-name'), {
      target: { value: 'healthz' },
    })
    fireEvent.change(screen.getByTestId('add-probe-target'), {
      target: { value: 'http://localhost:8080' },
    })

    const submitButton = screen.getByTestId('add-probe-submit')
    expect(submitButton).not.toBeDisabled()
    fireEvent.click(submitButton)

    expect(mutate).toHaveBeenCalled()
  })

  it('shows name validation error when name is invalid', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-name'), {
      target: { value: 'invalid name!' },
    })
    expect(screen.getByText('Name must match ^[a-zA-Z0-9_-]{1,64}$')).toBeInTheDocument()
    expect(screen.getByTestId('add-probe-submit')).toBeDisabled()
  })

  it('shows interval validation error when interval is out of range', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-name'), { target: { value: 'healthz' } })
    fireEvent.change(screen.getByTestId('add-probe-target'), {
      target: { value: 'http://localhost' },
    })
    fireEvent.change(screen.getByTestId('add-probe-interval'), { target: { value: '9999' } })
    expect(screen.getByText('Interval must be 1-3600')).toBeInTheDocument()
    expect(screen.getByTestId('add-probe-submit')).toBeDisabled()
  })

  it('shows timeout validation error when timeout is out of range', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-name'), { target: { value: 'healthz' } })
    fireEvent.change(screen.getByTestId('add-probe-target'), {
      target: { value: 'http://localhost' },
    })
    fireEvent.change(screen.getByTestId('add-probe-timeout'), { target: { value: '9999' } })
    expect(screen.getByText('Timeout must be 1-300')).toBeInTheDocument()
    expect(screen.getByTestId('add-probe-submit')).toBeDisabled()
  })

  it('calls onOpenChange(false) when Cancel is clicked', () => {
    const onOpenChange = vi.fn()
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={onOpenChange} />)
    fireEvent.click(screen.getByText('Cancel'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('shows Saving… and disables submit when isPending is true', () => {
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: true,
    } as never)
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    expect(screen.getByText('Saving…')).toBeInTheDocument()
    expect(screen.getByTestId('add-probe-submit')).toBeDisabled()
  })

  it('shows error toast when mutateAsync throws ApiError', async () => {
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'conflict',
        message: 'Conflict',
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-name'), { target: { value: 'healthz' } })
    fireEvent.change(screen.getByTestId('add-probe-target'), {
      target: { value: 'http://localhost:8080' },
    })
    fireEvent.click(screen.getByTestId('add-probe-submit'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Conflict')
    })
  })

  it('shows generic error toast when mutateAsync throws non-ApiError', async () => {
    const mutate = vi.fn().mockRejectedValue(new Error('network'))
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-name'), { target: { value: 'healthz' } })
    fireEvent.change(screen.getByTestId('add-probe-target'), {
      target: { value: 'http://localhost:8080' },
    })
    fireEvent.click(screen.getByTestId('add-probe-submit'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Add failed')
    })
  })

  it('changes kind select and updates placeholder', () => {
    renderWithClient(<AddProbeModal containerName="web" open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('add-probe-kind'), { target: { value: 'tcp' } })
    expect(screen.getByTestId('add-probe-kind')).toHaveValue('tcp')
  })
})
