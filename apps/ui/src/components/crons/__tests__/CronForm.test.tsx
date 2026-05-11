import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronForm } from '@/components/crons/CronForm'

afterEach(cleanup)

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient()
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('CronForm', () => {
  it('renders all required fields in create mode', () => {
    wrap(<CronForm mode="create" onSubmit={vi.fn()} />)
    expect(screen.getByLabelText(/Name/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Host/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Command/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Integration mode/i)).toBeInTheDocument()
  })

  it('shows validation error when submitting with empty required fields', async () => {
    const onSubmit = vi.fn()
    wrap(<CronForm mode="create" onSubmit={onSubmit} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /Create/i }))
    expect(screen.getByText(/Name is required/i)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('calls onSubmit with mapped payload when valid', async () => {
    const onSubmit = vi.fn()
    wrap(<CronForm mode="create" onSubmit={onSubmit} />)
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/Name/i), 'my-cron')
    await user.type(screen.getByLabelText(/Host/i), 'host-x')
    await user.type(screen.getByLabelText(/Command/i), '/opt/job')
    await user.type(screen.getByRole('textbox', { name: /Cron expression/i }), '*/5 * * * *')
    await user.click(screen.getByRole('button', { name: /Create/i }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    const arg = onSubmit.mock.calls[0]![0] as Record<string, unknown>
    expect(arg.name).toBe('my-cron')
    expect(arg.host).toBe('host-x')
    expect(arg.command).toBe('/opt/job')
    expect(arg.schedule).toBe('*/5 * * * *')
    expect(arg.cadence_seconds).toBe(0)
  })

  it('switches to cadence mode and disables schedule field', async () => {
    wrap(<CronForm mode="create" onSubmit={vi.fn()} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('radio', { name: /Cadence \(seconds\)/i }))
    // Schedule input should be replaced with a number input
    expect(screen.getByLabelText(/Cadence seconds/i)).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /Cron expression/i })).toBeNull()
  })

  it('renders edit mode with default values', () => {
    wrap(
      <CronForm
        mode="edit"
        defaultValues={{
          id: 'c1',
          name: 'existing',
          host: 'h',
          command: '/x',
          schedule: '* * * * *',
          schedule_canonical: '* * * * *',
          cadence_seconds: 0,
          expected_grace_seconds: 300,
          integration_mode: 'observe',
          enabled: true,
          last_seen_state: 'ok',
          created_at: '',
          updated_at: '',
          archived_at: null,
        }}
        onSubmit={vi.fn()}
      />,
    )
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    expect((screen.getByLabelText(/Name/i) as HTMLInputElement).value).toBe('existing')
    expect(screen.getByRole('button', { name: /Save/i })).toBeInTheDocument()
  })
})
