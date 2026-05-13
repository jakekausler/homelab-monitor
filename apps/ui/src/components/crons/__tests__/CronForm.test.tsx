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
  it('renders name, grace, and enabled fields', () => {
    wrap(<CronForm onSubmit={vi.fn()} />)
    expect(screen.getByLabelText(/Name/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Expected grace/i)).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).toBeInTheDocument()
  })

  it('does not render host, command, schedule, or cadence fields', () => {
    wrap(<CronForm onSubmit={vi.fn()} />)
    expect(screen.queryByLabelText(/Host/i)).toBeNull()
    expect(screen.queryByLabelText(/Command/i)).toBeNull()
    expect(screen.queryByRole('radio')).toBeNull()
    expect(screen.queryByLabelText(/Cron expression/i)).toBeNull()
    expect(screen.queryByLabelText(/Cadence seconds/i)).toBeNull()
  })

  it('shows validation error when submitting with empty name', async () => {
    const onSubmit = vi.fn()
    wrap(<CronForm onSubmit={onSubmit} />)
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText(/Name/i))
    await user.click(screen.getByRole('button', { name: /Save/i }))
    expect(screen.getByText(/Name is required/i)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('calls onSubmit with CronUpdate-shaped payload', async () => {
    const onSubmit = vi.fn()
    wrap(
      <CronForm
        onSubmit={onSubmit}
        defaultValues={{
          fingerprint: 'c'.repeat(64),
          name: 'existing',
          expected_grace_seconds: 300,
          enabled: true,
          host: 'h',
          command: '/x',
          schedule: '* * * * *',
          schedule_canonical: '* * * * *',
          cadence_seconds: 0,
          last_seen_state: 'ok',
          created_at: '',
          updated_at: '',
          hidden_at: null,
          source_path: null,
          wrapper_last_seen_at: null,
        }}
      />,
    )
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText(/Name/i))
    await user.type(screen.getByLabelText(/Name/i), 'renamed')
    await user.click(screen.getByRole('button', { name: /Save/i }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    const arg = onSubmit.mock.calls[0]![0] as Record<string, unknown>
    expect(arg.name).toBe('renamed')
    expect(arg.expected_grace_seconds).toBe(300)
    expect(arg.enabled).toBe(true)
    // Must NOT include create-only fields
    expect(arg.host).toBeUndefined()
    expect(arg.command).toBeUndefined()
    expect(arg.schedule).toBeUndefined()
    expect(arg.cadence_seconds).toBeUndefined()
  })

  it('renders with default values populated', () => {
    wrap(
      <CronForm
        defaultValues={{
          fingerprint: 'c'.repeat(64),
          name: 'existing',
          host: 'h',
          command: '/x',
          schedule: '* * * * *',
          schedule_canonical: '* * * * *',
          cadence_seconds: 0,
          expected_grace_seconds: 300,
          enabled: true,
          last_seen_state: 'ok',
          created_at: '',
          updated_at: '',
          hidden_at: null,
          source_path: null,
          wrapper_last_seen_at: null,
        }}
        onSubmit={vi.fn()}
      />,
    )
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion
    expect((screen.getByLabelText(/Name/i) as HTMLInputElement).value).toBe('existing')
    expect(screen.getByRole('button', { name: /Save/i })).toBeInTheDocument()
  })
})
