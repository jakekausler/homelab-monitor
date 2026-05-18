import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { InstallHeartbeatModal } from '@/components/crons/InstallHeartbeatModal'

afterEach(cleanup)

// ---------------------------------------------------------------------------
// Mock hooks + toast
// ---------------------------------------------------------------------------

vi.mock('@/api/crons', () => ({
  useInstallWrapper: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

// Must come after vi.mock
import { useInstallWrapper } from '@/api/crons'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FP = 'b'.repeat(64)

const previewData = {
  wrapper_content: '#!/bin/bash\n# wrapper script content',
  crontab_diff: {
    source_path: '/etc/cron.d/backup',
    old_line: '0 4 * * * root /opt/backup.sh',
    new_line: '0 4 * * * root /opt/hm-wrapper.sh backup',
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
      <InstallHeartbeatModal
        fingerprint={FP}
        open={props.open ?? true}
        onOpenChange={props.onOpenChange ?? vi.fn()}
      />
    </QueryClientProvider>,
  )
}

// Waits until the crontab diff section has loaded (preview data is visible)
async function waitForPreview() {
  return screen.findByText('Crontab diff')
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InstallHeartbeatModal', () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockClear()
    vi.mocked(toast.error).mockClear()
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation() as unknown as ReturnType<typeof useInstallWrapper>,
    )
  })

  // 1. Renders the dialog title
  it('renders the dialog title when open', async () => {
    renderModal({})
    expect(await screen.findByText('Install heartbeat wrapper')).toBeInTheDocument()
  })

  // 2. Fires dry-run (confirm: false) on open and renders preview content
  it('fires dry-run on open and renders crontab diff + wrapper script', async () => {
    const mutateAsync = vi.fn().mockResolvedValue(previewData)
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    renderModal({})
    // Wait for preview section heading
    await waitForPreview()
    // source_path appears as "File: /etc/cron.d/backup" — use regex
    expect(screen.getByText(/\/etc\/cron\.d\/backup/)).toBeInTheDocument()
    // old_line and new_line are in <code> elements
    expect(screen.getByText('0 4 * * * root /opt/backup.sh')).toBeInTheDocument()
    expect(screen.getByText('0 4 * * * root /opt/hm-wrapper.sh backup')).toBeInTheDocument()
    // Wrapper script section heading
    expect(screen.getByText('Wrapper script')).toBeInTheDocument()
    // dry-run called with confirm: false
    expect(mutateAsync).toHaveBeenCalledWith({ confirm: false })
  })

  // 3. Install button is DISABLED until the confirmation checkbox is checked
  it('Install button is disabled before checkbox is checked', async () => {
    renderModal({})
    await waitForPreview()
    const installBtn = screen.getByRole('button', { name: 'Install' })
    expect(installBtn).toBeDisabled()
  })

  // 4. Checking the checkbox enables the Install button
  it('Install button becomes enabled after checking the confirmation checkbox', async () => {
    renderModal({})
    await waitForPreview()
    const checkbox = screen.getByRole('checkbox')
    await userEvent.setup().click(checkbox)
    expect(screen.getByRole('button', { name: 'Install' })).toBeEnabled()
  })

  // 5. Clicking Install fires confirm POST (confirm: true) and shows success toast
  it('clicking Install calls mutateAsync with confirm: true and shows success toast', async () => {
    const mutateAsync = vi
      .fn()
      .mockResolvedValueOnce(previewData) // dry-run
      .mockResolvedValueOnce({ installed: true }) // confirm
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    const onOpenChange = vi.fn()
    renderModal({ onOpenChange })
    await waitForPreview()
    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledWith({ confirm: true })
      expect(toast.success).toHaveBeenCalledWith('Heartbeat wrapper installed')
    })
  })

  // 6. 409 error shows line-not-found / already-wrapped toast
  it('shows "Line not found or already wrapped" toast on 409 error', async () => {
    const err409 = new ApiError({
      status: 409,
      code: 'conflict',
      message: 'Conflict',
      retryAfterSeconds: null,
      details: null,
    })
    const mutateAsync = vi.fn().mockResolvedValueOnce(previewData).mockRejectedValueOnce(err409)
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    renderModal({})
    await waitForPreview()
    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Line not found or already wrapped')
    })
  })

  // 7. 400 error shows remote-host toast
  it('shows "Cannot install on remote host" toast on 400 error', async () => {
    const err400 = new ApiError({
      status: 400,
      code: 'bad_request',
      message: 'Bad Request',
      retryAfterSeconds: null,
      details: null,
    })
    const mutateAsync = vi.fn().mockResolvedValueOnce(previewData).mockRejectedValueOnce(err400)
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    renderModal({})
    await waitForPreview()
    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Cannot install on remote host')
    })
  })

  // 8. Non-ApiError shows generic "Install failed" toast
  it('shows "Install failed" toast for non-ApiError rejection', async () => {
    const genericErr = new Error('Network error')
    const mutateAsync = vi.fn().mockResolvedValueOnce(previewData).mockRejectedValueOnce(genericErr)
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    renderModal({})
    await waitForPreview()
    const user = userEvent.setup()
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('Install failed')
    })
  })

  // 9. Loading state: shows "Loading preview…" when isPending and no previewData
  it('shows Loading preview text while dry-run is pending', async () => {
    // mutateAsync never resolves → isPending stays true, no previewData
    const mutateAsync = vi.fn().mockReturnValue(new Promise(() => undefined))
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({ mutateAsync, isPending: true }) as unknown as ReturnType<
        typeof useInstallWrapper
      >,
    )
    renderModal({})
    expect(await screen.findByText('Loading preview…')).toBeInTheDocument()
  })

  // 10. Cancel button calls onOpenChange(false)
  it('Cancel button calls onOpenChange(false)', async () => {
    const onOpenChange = vi.fn()
    renderModal({ onOpenChange })
    await waitForPreview()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  // 11. Modal not shown when open=false
  it('does not render dialog content when open is false', () => {
    renderModal({ open: false })
    expect(screen.queryByText('Install heartbeat wrapper')).toBeNull()
  })

  // M7 (item 17): failed preview renders role="alert" + Close button, NOT empty dialog
  it('renders a role="alert" error and Close button when preview fails', async () => {
    const failingMutate = vi.fn().mockRejectedValue(new Error('Network error'))
    vi.mocked(useInstallWrapper).mockReturnValue(
      makeMutation({
        mutateAsync: failingMutate,
        isPending: false,
        error: new Error('Network error'),
      }) as unknown as ReturnType<typeof useInstallWrapper>,
    )
    renderModal({})
    // Wait for the error state to appear (hasLoadedPreview=true, previewData=null)
    const alert = await screen.findByRole('alert')
    expect(alert).toBeInTheDocument()
    // Close button must be present
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
    // The dialog content must NOT be empty — title is still visible
    expect(screen.getByText('Install heartbeat wrapper')).toBeInTheDocument()
  })
})
