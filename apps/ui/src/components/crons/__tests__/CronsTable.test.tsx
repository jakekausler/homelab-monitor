import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { CronsTable } from '@/components/crons/CronsTable'
import type { components } from '@/api/schema'

type CronOut = components['schemas']['CronOut']

afterEach(cleanup)

function renderInRouter(ui: React.ReactNode) {
  const rootRoute = createRootRoute({ component: () => <>{ui}</> })
  const cronRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/inventory/crons/$cronId',
    component: () => null,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([cronRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

const sampleCron: CronOut = {
  fingerprint: 'b'.repeat(64),
  name: 'daily-backup',
  host: 'host-a',
  command: '/opt/backup.sh',
  schedule: '0 4 * * *',
  schedule_canonical: '0 4 * * *',
  cadence_seconds: 0,
  expected_grace_seconds: 300,
  enabled: true,
  is_local: true,
  last_seen_state: 'ok',
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  hidden_at: null,
  soft_deleted_at: null,
  source_path: null,
  wrapper_last_seen_at: null,
  last_discovered_at: null,
}

describe('CronsTable', () => {
  it('renders empty hint when items is empty', async () => {
    renderInRouter(<CronsTable items={[]} isLoading={false} />)
    expect(await screen.findByText(/No crons yet/i)).toBeInTheDocument()
  })

  it('renders loading state', async () => {
    renderInRouter(<CronsTable items={[]} isLoading={true} />)
    expect(await screen.findByText(/Loading crons/i)).toBeInTheDocument()
  })

  it('renders rows with name, host, schedule, state', async () => {
    renderInRouter(<CronsTable items={[sampleCron]} isLoading={false} />)
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.getByText('host-a')).toBeInTheDocument()
    expect(screen.getByText('0 4 * * *')).toBeInTheDocument()
    expect(screen.getByText('Ok')).toBeInTheDocument()
  })

  it('renders cadence-only crons with "every Xs" text', async () => {
    const cadenceCron: CronOut = { ...sampleCron, schedule: null, cadence_seconds: 60 }
    renderInRouter(<CronsTable items={[cadenceCron]} isLoading={false} />)
    expect(await screen.findByText('every 60s')).toBeInTheDocument()
  })

  it('renders Hidden badge for hidden crons', async () => {
    const hidden: CronOut = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    renderInRouter(<CronsTable items={[hidden]} isLoading={false} />)
    expect(await screen.findByRole('cell', { name: 'Hidden' })).toBeInTheDocument()
  })

  it('shows Remote badge when source_path is null', async () => {
    renderInRouter(<CronsTable items={[{ ...sampleCron, source_path: null }]} isLoading={false} />)
    expect(await screen.findByText('Remote')).toBeInTheDocument()
  })

  it('does not show Remote badge when source_path is set', async () => {
    renderInRouter(
      <CronsTable
        items={[{ ...sampleCron, source_path: '/etc/cron.d/backup' }]}
        isLoading={false}
      />,
    )
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.queryByText('Remote')).toBeNull()
  })

  it('shows wrapper checkmark when wrapper_last_seen_at is set', async () => {
    const withWrapper: CronOut = { ...sampleCron, wrapper_last_seen_at: '2026-05-10T00:00:00Z' }
    renderInRouter(<CronsTable items={[withWrapper]} isLoading={false} />)
    expect(await screen.findByLabelText('Wrapper installed')).toBeInTheDocument()
  })

  it('does not show wrapper checkmark when wrapper_last_seen_at is null', async () => {
    renderInRouter(
      <CronsTable items={[{ ...sampleCron, wrapper_last_seen_at: null }]} isLoading={false} />,
    )
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.queryByLabelText('Wrapper installed')).toBeNull()
  })

  it('shows Hidden badge when hidden_at is set', async () => {
    const hidden: CronOut = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    renderInRouter(<CronsTable items={[hidden]} isLoading={false} />)
    expect(await screen.findByRole('cell', { name: 'Hidden' })).toBeInTheDocument()
  })

  it('renders Soft-deleted badge when soft_deleted_at is set', async () => {
    const softDeleted: CronOut = { ...sampleCron, soft_deleted_at: '2026-05-12T00:00:00Z' }
    renderInRouter(<CronsTable items={[softDeleted]} isLoading={false} />)
    expect(await screen.findByTestId('soft-deleted-badge')).toBeInTheDocument()
  })

  it('does not render Soft-deleted badge when soft_deleted_at is null', async () => {
    renderInRouter(<CronsTable items={[sampleCron]} isLoading={false} />)
    expect(await screen.findByText('daily-backup')).toBeInTheDocument()
    expect(screen.queryByTestId('soft-deleted-badge')).toBeNull()
  })

  it('applies opacity-60 class to soft-deleted rows', async () => {
    const softDeleted: CronOut = { ...sampleCron, soft_deleted_at: '2026-05-12T00:00:00Z' }
    renderInRouter(<CronsTable items={[softDeleted]} isLoading={false} />)
    const badge = await screen.findByTestId('soft-deleted-badge')
    // The <tr> is the closest row ancestor
    const row = badge.closest('tr')
    expect(row).toHaveClass('opacity-60')
  })

  it('renders updated empty state copy', async () => {
    renderInRouter(<CronsTable items={[]} isLoading={false} />)
    expect(
      await screen.findByText(
        'No crons yet. Crons will appear here once they are discovered or have registered a heartbeat.',
      ),
    ).toBeInTheDocument()
  })
})
