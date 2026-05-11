import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AddCronModal } from '@/components/crons/AddCronModal'

afterEach(cleanup)

vi.mock('@/api/crons', () => ({
  useCreateCron: vi.fn(() => ({
    mutateAsync: vi.fn(),
    isPending: false,
  })),
  usePreviewExpr: vi.fn(() => ({ isLoading: false, error: null, data: null })),
  cronQueryKeys: { all: ['crons'] },
}))

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient()
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('AddCronModal', () => {
  it('does not render dialog content when closed', () => {
    wrap(<AddCronModal open={false} onOpenChange={vi.fn()} />)
    expect(screen.queryByText(/Add cron/i)).toBeNull()
  })

  it('renders form when open', () => {
    wrap(<AddCronModal open={true} onOpenChange={vi.fn()} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/Add cron/i)).toBeInTheDocument()
  })

  it('renders CronForm inside the dialog', () => {
    wrap(<AddCronModal open={true} onOpenChange={vi.fn()} />)
    expect(screen.getByLabelText(/Name/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Host/i)).toBeInTheDocument()
  })

  it('calls onOpenChange(false) when cancel is clicked', async () => {
    const onOpenChange = vi.fn()
    wrap(<AddCronModal open={true} onOpenChange={onOpenChange} />)
    await userEvent.setup().click(screen.getByRole('button', { name: /Cancel/i }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('calls mutateAsync and closes modal on successful submit', async () => {
    const { useCreateCron } = await import('@/api/crons')
    const mutateAsync = vi.fn().mockResolvedValue({})
    vi.mocked(useCreateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateCron>)
    const onOpenChange = vi.fn()
    wrap(<AddCronModal open={true} onOpenChange={onOpenChange} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Name/i), 'my-cron')
    await user.type(screen.getByLabelText(/Host/i), 'host-x')
    await user.type(screen.getByLabelText(/Command/i), '/opt/job')
    await user.type(screen.getByRole('textbox', { name: /Cron expression/i }), '*/5 * * * *')
    await user.click(screen.getByRole('button', { name: /Create/i }))
    expect(mutateAsync).toHaveBeenCalledTimes(1)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('shows error message when mutateAsync throws', async () => {
    const { useCreateCron } = await import('@/api/crons')
    const mutateAsync = vi.fn().mockRejectedValue(new Error('Unexpected error creating cron.'))
    vi.mocked(useCreateCron).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateCron>)
    wrap(<AddCronModal open={true} onOpenChange={vi.fn()} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Name/i), 'bad')
    await user.type(screen.getByLabelText(/Host/i), 'h')
    await user.type(screen.getByLabelText(/Command/i), '/x')
    await user.type(screen.getByRole('textbox', { name: /Cron expression/i }), '* * * * *')
    await user.click(screen.getByRole('button', { name: /Create/i }))
    expect(await screen.findByText(/Unexpected error/i)).toBeInTheDocument()
  })
})
