import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfirmDeleteModal } from '@/components/crons/ConfirmDeleteModal'

afterEach(cleanup)

describe('ConfirmDeleteModal', () => {
  it('does not render when closed', () => {
    render(
      <ConfirmDeleteModal
        open={false}
        onOpenChange={vi.fn()}
        cronName="daily-backup"
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.queryByText(/Soft-delete cron/i)).toBeNull()
  })

  it('renders cron name in confirmation instruction', () => {
    render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="daily-backup"
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByText('daily-backup')).toBeInTheDocument()
  })

  it('confirm button is disabled until cron name is typed', async () => {
    render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="daily-backup"
        onConfirm={vi.fn()}
      />,
    )
    const btn = screen.getByRole('button', { name: /Soft-delete/i })
    expect(btn).toBeDisabled()

    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox'), 'daily-backup')
    expect(btn).toBeEnabled()
  })

  it('calls onConfirm when confirm button clicked after typing name', async () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="my-cron"
        onConfirm={onConfirm}
      />,
    )
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox'), 'my-cron')
    await user.click(screen.getByRole('button', { name: /Soft-delete/i }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('shows error message when errorMessage prop is set', () => {
    render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="my-cron"
        onConfirm={vi.fn()}
        errorMessage="Delete failed"
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Delete failed')
  })

  it('shows Deleting… on the button when isDeleting is true', async () => {
    render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="my-cron"
        onConfirm={vi.fn()}
        isDeleting={true}
      />,
    )
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox'), 'my-cron')
    expect(screen.getByRole('button', { name: /Deleting/i })).toBeInTheDocument()
  })

  it('resets typed value when dialog closes', async () => {
    const { rerender } = render(
      <ConfirmDeleteModal
        open={true}
        onOpenChange={vi.fn()}
        cronName="my-cron"
        onConfirm={vi.fn()}
      />,
    )
    await userEvent.setup().type(screen.getByRole('textbox'), 'my-cron')
    rerender(
      <ConfirmDeleteModal
        open={false}
        onOpenChange={vi.fn()}
        cronName="my-cron"
        onConfirm={vi.fn()}
      />,
    )
    // Dialog is closed, no input visible
    expect(screen.queryByRole('textbox')).toBeNull()
  })
})
