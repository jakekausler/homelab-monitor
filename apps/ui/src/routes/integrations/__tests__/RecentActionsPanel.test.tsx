import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RecentActionsPanel } from '@/routes/integrations/RecentActionsPanel'

vi.mock('@/api/docker', () => ({
  useListComposeActions: vi.fn(),
}))

import { useListComposeActions } from '@/api/docker'

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

describe('RecentActionsPanel', () => {
  it('renders empty state when no actions', () => {
    vi.mocked(useListComposeActions).mockReturnValue({
      data: { actions: [] },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(wrap(<RecentActionsPanel containerName="caddy" />))
    expect(screen.getByText(/no recent actions/i)).toBeInTheDocument()
  })

  it('renders action rows with state badge + container', () => {
    vi.mocked(useListComposeActions).mockReturnValue({
      data: {
        actions: [
          {
            action_id: 1,
            action: 'pull_and_restart',
            container_name: 'caddy',
            compose_service: 'caddy',
            command: 'docker compose pull caddy',
            state: 'success',
            started_at: '2026-01-01T00:00:00+00:00',
            ended_at: '2026-01-01T00:00:03+00:00',
            duration_seconds: 3.0,
            who: 'alice',
            client_ip: null,
            stdout: null,
            stderr: null,
            exit_code: 0,
            error_reason: null,
            before_image: null,
            before_digest: null,
            after_image: null,
            after_digest: null,
          },
        ],
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(wrap(<RecentActionsPanel containerName="caddy" />))
    expect(screen.getByText('success')).toBeInTheDocument()
    expect(screen.getByText('caddy')).toBeInTheDocument()
    expect(screen.getByText('by alice')).toBeInTheDocument()
  })

  it('expands to show command and outputs when clicked', () => {
    vi.mocked(useListComposeActions).mockReturnValue({
      data: {
        actions: [
          {
            action_id: 1,
            action: 'pull_and_restart',
            container_name: 'caddy',
            compose_service: 'caddy',
            command: 'docker compose pull caddy',
            state: 'success',
            started_at: '2026-01-01T00:00:00+00:00',
            ended_at: '2026-01-01T00:00:03+00:00',
            duration_seconds: 3.0,
            who: 'alice',
            client_ip: null,
            stdout: 'pulled\n',
            stderr: '',
            exit_code: 0,
            error_reason: null,
            before_image: null,
            before_digest: null,
            after_image: null,
            after_digest: null,
          },
        ],
      },
      isPending: false,
      isError: false,
      error: null,
    } as never)
    render(wrap(<RecentActionsPanel containerName="caddy" />))
    const buttons = screen.getAllByRole('button')
    const toggle = buttons[0] as HTMLButtonElement
    fireEvent.click(toggle)
    expect(screen.getByText('docker compose pull caddy')).toBeInTheDocument()
  })
})
