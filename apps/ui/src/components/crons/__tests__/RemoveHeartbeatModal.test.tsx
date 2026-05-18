import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RemoveHeartbeatModal } from '@/components/crons/RemoveHeartbeatModal'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mock hooks + toast
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useUninstallWrapper: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

// Must come after vi.mock
import { useUninstallWrapper } from '@/api/crons'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FP = 'c'.repeat(64)

const previewData = {
  fingerprint: FP,
  crontab_diff: {
    source_path: '/etc/crontab',
    old_line: '0 4 * * * root /usr/local/bin/cron-with-heartbeat.sh -- /opt/backup.sh',
    new_line: '0 4 * * * root /opt/backup.sh',
  },
}

function makeMutation(
  overrides: Partial<{
    mutateAsync: ReturnType<typeof vi.fn>
    isPending: boolean
    error: Error | null
  }> = {},
) {
  return {
    mutateAsync: vi.fn().mockResolvedValue(previewData),
    isPending: false,
    error: null,
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function renderModal(props: { open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <RemoveHeartbeatModal
        fingerprint={FP}
        open={props.open ?? true}
        onOpenChange={props.onOpenChange ?? vi.fn()}
      />
    </QueryClientProvider>,
  )
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RemoveHeartbeatModal', () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(useUninstallWrapper).mockReturnValue(
      makeMutation() as unknown as ReturnType<typeof useUninstallWrapper>,
    )
  })

  // 1. Renders the dialog title
  it('renders the dialog title when open', async () => {
    renderModal({})
    expect(await screen.findByText('Remove heartbeat wrapper')).toBeInTheDocument()
  })

  // 2. Shows the crontab diff after the preview loads
  it('shows the crontab diff section after preview loads', async () => {
    renderModal({})
    expect(await screen.findByText('Crontab diff')).toBeInTheDocument()
  })

  // 3. Old line is visible in diff
  it('shows old_line in the crontab diff', async () => {
    renderModal({})
    await screen.findByText('Crontab diff')
    expect(screen.getByText(previewData.crontab_diff.old_line)).toBeInTheDocument()
  })

  // 4. New line is visible in diff
  it('shows new_line in the crontab diff', async () => {
    renderModal({})
    await screen.findByText('Crontab diff')
    expect(screen.getByText(previewData.crontab_diff.new_line)).toBeInTheDocument()
  })

  // 5. Remove button disabled until checkbox checked
  it('Remove button is disabled until confirmation checkbox is checked', async () => {
    renderModal({})
    await screen.findByText('Crontab diff')
    const removeBtn = screen.getByRole('button', { name: 'Remove' })
    expect(removeBtn).toBeDisabled()
  })

  // 6. Remove button enabled after checkbox checked
  it('Remove button is enabled after checking the confirmation checkbox', async () => {
    const user = userEvent.setup()
    renderModal({})
    await screen.findByText('Crontab diff')
    const checkbox = screen.getByRole('checkbox')
    await user.click(checkbox)
    const removeBtn = screen.getByRole('button', { name: 'Remove' })
    expect(removeBtn).toBeEnabled()
  })

  // 7. Clicking Remove calls mutateAsync with confirm=true + shows success toast
  it('calls mutateAsync with confirm=true on Remove click and shows success toast', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    const mockMutateAsync = vi
      .fn()
      .mockResolvedValueOnce(previewData) // dry-run call on open
      .mockResolvedValueOnce({ cron: {} }) // confirm call

    vi.mocked(useUninstallWrapper).mockReturnValue({
      mutateAsync: mockMutateAsync,
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useUninstallWrapper>)

    renderModal({ onOpenChange })
    await screen.findByText('Crontab diff')
    const checkbox = screen.getByRole('checkbox')
    await user.click(checkbox)
    const removeBtn = screen.getByRole('button', { name: 'Remove' })
    await user.click(removeBtn)

    expect(mockMutateAsync).toHaveBeenCalledWith({ confirm: true })
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Heartbeat wrapper removed')
  })

  // 8. 409 error → toast with "not found or not wrapped"
  it('shows "Line not found or not wrapped" toast on 409 error', async () => {
    const user = userEvent.setup()
    const err = new ApiError({
      status: 409,
      code: 'not_wrapped',
      message: 'not wrapped',
      retryAfterSeconds: null,
      details: null,
    })
    const mockMutateAsync = vi.fn().mockResolvedValueOnce(previewData).mockRejectedValueOnce(err)

    vi.mocked(useUninstallWrapper).mockReturnValue({
      mutateAsync: mockMutateAsync,
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useUninstallWrapper>)

    renderModal({})
    await screen.findByText('Crontab diff')
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Remove' }))

    expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Line not found or not wrapped')
  })

  // 9. 400 error → toast about remote host
  it('shows "Cannot remove on remote host" toast on 400 error', async () => {
    const user = userEvent.setup()
    const err = new ApiError({
      status: 400,
      code: 'remote_host',
      message: 'remote host',
      retryAfterSeconds: null,
      details: null,
    })
    const mockMutateAsync = vi.fn().mockResolvedValueOnce(previewData).mockRejectedValueOnce(err)

    vi.mocked(useUninstallWrapper).mockReturnValue({
      mutateAsync: mockMutateAsync,
      isPending: false,
      error: null,
    } as unknown as ReturnType<typeof useUninstallWrapper>)

    renderModal({})
    await screen.findByText('Crontab diff')
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Remove' }))

    expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Cannot remove on remote host')
  })

  // 10. Preview load failure → shows error message
  it('shows error state when preview fails to load', async () => {
    const mockMutateAsync = vi.fn().mockRejectedValueOnce(new Error('network error'))

    vi.mocked(useUninstallWrapper).mockReturnValue({
      mutateAsync: mockMutateAsync,
      isPending: false,
      error: new Error('network error'),
    } as unknown as ReturnType<typeof useUninstallWrapper>)

    renderModal({})
    // The modal should show the error state (fallback message or API error message)
    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
  })

  // 11. Cancel button closes the modal
  it('Cancel button calls onOpenChange(false)', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    renderModal({ onOpenChange })
    await screen.findByText('Crontab diff')
    const cancelBtn = screen.getByRole('button', { name: 'Cancel' })
    await user.click(cancelBtn)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
