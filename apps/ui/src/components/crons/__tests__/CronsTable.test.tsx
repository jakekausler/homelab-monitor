import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CronsTable } from '@/components/crons/CronsTable'
import type { components } from '@/api/schema'

type CronOut = components['schemas']['CronOut']

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
  last_seen_state: 'ok',
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
  hidden_at: null,
  source_path: null,
  wrapper_installed_at: null,
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

  it('renders archived badge for hidden crons', async () => {
    const hidden: CronOut = { ...sampleCron, hidden_at: '2026-05-10T00:00:00Z' }
    renderInRouter(<CronsTable items={[hidden]} isLoading={false} />)
    expect(await screen.findByText('archived')).toBeInTheDocument()
  })
})
