import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'

import { ContainerProbesCard } from '../ContainerProbesCard'
import {
  useListProbes,
  useListDockerSuggestions,
  useSuggestionDefaultProbes,
  useCreateProbeTarget,
  useDeleteProbeTarget,
  useUpdateProbeTarget,
} from '@/api/docker'
import { ApiError } from '@/api/client'

vi.mock('@/api/docker', () => ({
  useListProbes: vi.fn(),
  useListDockerSuggestions: vi.fn(),
  useSuggestionDefaultProbes: vi.fn(),
  useCreateProbeTarget: vi.fn(),
  useDeleteProbeTarget: vi.fn(),
  useUpdateProbeTarget: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

function renderWithClient(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ContainerProbesCard', () => {
  beforeEach(() => {
    vi.mocked(useListProbes).mockReturnValue({
      data: { probes: [] },
      isLoading: false,
      isError: false,
    } as never)
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: { pages: [{ suggestions: [] }] },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: undefined,
      isLoading: false,
    } as never)
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
    vi.mocked(useUpdateProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as never)
  })

  afterEach(() => {
    cleanup()
  })

  const mockContainer = {
    id: 'test-id',
    name: 'web',
    image: 'nginx:latest',
    status: 'running' as const,
    labels: {},
  }

  it('renders container header with name and image', () => {
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByText('web')).toBeInTheDocument()
    expect(screen.getByText('nginx:latest')).toBeInTheDocument()
  })

  it('renders "No active probes" when empty', () => {
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('no-active-probes')).toBeInTheDocument()
  })

  it('renders active probes with Edit and Delete buttons', () => {
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('active-probes-list')).toBeInTheDocument()
    expect(screen.getByTestId('active-probe-edit-probe-1')).toBeInTheDocument()
    expect(screen.getByTestId('active-probe-delete-probe-1')).toBeInTheDocument()
  })

  it('opens AddProbeModal when "Add new probe" is clicked', () => {
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('add-new-probe-button'))
    expect(screen.getByTestId('add-probe-modal')).toBeInTheDocument()
  })

  it('hides suggested probes section when no pending suggestion', () => {
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.queryByTestId('suggested-probes-list')).not.toBeInTheDocument()
  })

  it('shows suggested probes when suggestion exists and reason is available', () => {
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: {
        reason: 'available' as const,
        probes: [
          {
            kind: 'http' as const,
            name: 'status',
            target_value: 'http://localhost:8080/status',
            interval_seconds: 60,
            timeout_seconds: 10,
          },
        ],
      },
      isLoading: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('suggested-probe-http|status')).toBeInTheDocument()
  })

  it('hides suggested probe when Ignore is clicked', () => {
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: {
        reason: 'available' as const,
        probes: [
          {
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            interval_seconds: 60,
            timeout_seconds: 10,
          },
        ],
      },
      isLoading: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('suggested-probe-http|healthz')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('suggested-probe-ignore-http|healthz'))
    expect(screen.queryByTestId('suggested-probe-http|healthz')).not.toBeInTheDocument()
  })

  it('renders loading state for active probes', () => {
    vi.mocked(useListProbes).mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByText('Loading active probes…')).toBeInTheDocument()
  })

  it('renders loading state for suggested probes when suggestion exists', () => {
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByText('Loading suggested defaults…')).toBeInTheDocument()
  })

  it('renders "No suggested probes" when filteredSuggested is empty', () => {
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: { reason: 'available' as const, probes: [] },
      isLoading: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('no-suggested-probes')).toBeInTheDocument()
  })

  it('calls deleteMutation.mutateAsync when Delete is clicked', () => {
    const deleteMutate = vi.fn().mockResolvedValue(null)
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: deleteMutate,
      isPending: false,
    } as never)
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('active-probe-delete-probe-1'))
    expect(deleteMutate).toHaveBeenCalled()
  })

  it('Delete button is disabled when deleteMutation isPending', () => {
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: true,
    } as never)
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('active-probe-delete-probe-1')).toBeDisabled()
  })

  it('opens EditProbeModal when Edit is clicked', () => {
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('active-probe-edit-probe-1'))
    expect(screen.getByTestId('edit-probe-modal')).toBeInTheDocument()
  })

  it('calls createMutation when Add is clicked on a suggested probe', () => {
    const createMutate = vi.fn().mockResolvedValue(null)
    vi.mocked(useCreateProbeTarget).mockReturnValue({
      mutateAsync: createMutate,
      isPending: false,
    } as never)
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: {
        reason: 'available' as const,
        probes: [
          {
            kind: 'http' as const,
            name: 'status',
            target_value: 'http://localhost:8080/status',
            interval_seconds: 60,
            timeout_seconds: 10,
          },
        ],
      },
      isLoading: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('suggested-probe-add-http|status'))
    expect(createMutate).toHaveBeenCalled()
  })

  it('shows error toast on delete ApiError', async () => {
    const deleteMutate = vi.fn().mockRejectedValue(
      new ApiError({
        status: 403,
        code: 'forbidden',
        message: 'Forbidden',
        retryAfterSeconds: null,
        details: null,
      }),
    )
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: deleteMutate,
      isPending: false,
    } as never)
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'healthz',
            target_value: 'http://localhost:8080/health',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('active-probe-delete-probe-1'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Forbidden')
    })
  })

  it('shows generic error toast on delete non-ApiError', async () => {
    const deleteMutate = vi.fn().mockRejectedValue(new Error('network'))
    vi.mocked(useDeleteProbeTarget).mockReturnValue({
      mutateAsync: deleteMutate,
      isPending: false,
    } as never)
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-2',
            container_name: 'web',
            kind: 'tcp' as const,
            name: 'port',
            target_value: 'tcp://localhost:5432',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 30,
            timeout_seconds: 5,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    fireEvent.click(screen.getByTestId('active-probe-delete-probe-2'))
    await vi.waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Delete failed')
    })
  })

  it('filters out already-active probes from suggested list', () => {
    vi.mocked(useListProbes).mockReturnValue({
      data: {
        probes: [
          {
            id: 'probe-1',
            container_name: 'web',
            kind: 'http' as const,
            name: 'status',
            target_value: 'http://localhost:8080/status',
            config_source: 'manual' as const,
            enabled: true,
            interval_seconds: 60,
            timeout_seconds: 10,
            last_run_at: null,
            last_status: null,
            last_error: null,
            created_at: '2026-05-26T00:00:00Z',
            hidden_at: null,
            exec_authorized: false,
          },
        ],
      },
      isLoading: false,
      isError: false,
    } as never)
    vi.mocked(useListDockerSuggestions).mockReturnValue({
      data: {
        pages: [{ suggestions: [{ id: 'sug-1', container_name: 'web' }] }],
      },
    } as never)
    vi.mocked(useSuggestionDefaultProbes).mockReturnValue({
      data: {
        reason: 'available' as const,
        probes: [
          {
            kind: 'http' as const,
            name: 'status',
            target_value: 'http://localhost:8080/status',
            interval_seconds: 60,
            timeout_seconds: 10,
          },
        ],
      },
      isLoading: false,
    } as never)
    renderWithClient(<ContainerProbesCard container={mockContainer} />)
    expect(screen.getByTestId('no-suggested-probes')).toBeInTheDocument()
  })
})
