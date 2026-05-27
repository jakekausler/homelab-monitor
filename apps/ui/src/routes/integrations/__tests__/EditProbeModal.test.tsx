import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Schema } from '@/api/types'

import { EditProbeModal } from '../EditProbeModal'
import { useUpdateProbeTarget } from '@/api/docker'
import { ApiError } from '@/api/client'
import { toast } from 'sonner'

type ProbeRow = Schema<'ProbeRow'>

vi.mock('@/api/docker', () => ({
  useUpdateProbeTarget: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('EditProbeModal', () => {
  const mockProbe: ProbeRow = {
    id: 'probe-1',
    container_name: 'web',
    kind: 'http',
    name: 'healthz',
    target_value: 'http://localhost:8080/health',
    config_source: 'manual',
    enabled: true,
    interval_seconds: 60,
    timeout_seconds: 10,
    last_run_at: null,
    last_status: null,
    last_error: null,
    created_at: '2026-05-26T00:00:00Z',
    hidden_at: null,
    exec_authorized: false,
  }

  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
  })

  it('renders modal when open', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('edit-probe-modal')).toBeInTheDocument()
  })

  it('shows kind and name as read-only', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('edit-probe-kind-readonly')).toHaveTextContent('http')
    expect(screen.getByTestId('edit-probe-name-readonly')).toHaveTextContent('healthz')
  })

  it('pre-fills target value from probe', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    expect(screen.getByTestId('edit-probe-target')).toHaveValue('http://localhost:8080/health')
  })

  it('disables submit when target is empty', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    const targetInput = screen.getByTestId('edit-probe-target')
    fireEvent.change(targetInput, { target: { value: '   ' } })
    expect(screen.getByText('Target is required')).toBeInTheDocument()
    expect(screen.getByTestId('edit-probe-submit')).toBeDisabled()
  })

  it('calls mutateAsync on valid submit', () => {
    const mutate = vi.fn().mockResolvedValue(null)
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)

    const targetInput = screen.getByTestId('edit-probe-target')
    fireEvent.change(targetInput, { target: { value: 'http://localhost:9090' } })

    fireEvent.click(screen.getByTestId('edit-probe-submit'))

    expect(mutate).toHaveBeenCalled()
  })

  it('shows interval validation error when interval is out of range', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('edit-probe-interval'), { target: { value: '9999' } })
    expect(screen.getByText('Interval must be 1-3600')).toBeInTheDocument()
    expect(screen.getByTestId('edit-probe-submit')).toBeDisabled()
  })

  it('shows timeout validation error when timeout is out of range', () => {
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId('edit-probe-timeout'), { target: { value: '9999' } })
    expect(screen.getByText('Timeout must be 1-300')).toBeInTheDocument()
    expect(screen.getByTestId('edit-probe-submit')).toBeDisabled()
  })

  it('calls onOpenChange(false) when Cancel is clicked', () => {
    const onOpenChange = vi.fn()
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={onOpenChange} />)
    fireEvent.click(screen.getByText('Cancel'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('shows Saving… and disables submit when isPending is true', () => {
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: true,
    } as never)
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    expect(screen.getByText('Saving…')).toBeInTheDocument()
    expect(screen.getByTestId('edit-probe-submit')).toBeDisabled()
  })

  it('shows error toast when mutateAsync throws ApiError', async () => {
    const mutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 404,
        code: 'not_found',
        message: 'Not found',
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('edit-probe-submit'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Not found')
    })
  })

  it('shows generic error toast when mutateAsync throws non-ApiError', async () => {
    const mutate = vi.fn().mockRejectedValue(new Error('network'))
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: mutate,
      isPending: false,
    } as never)
    renderWithClient(<EditProbeModal probe={mockProbe} open onOpenChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId('edit-probe-submit'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Update failed')
    })
  })
})
