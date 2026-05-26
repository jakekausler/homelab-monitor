import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import type { ReactElement } from 'react'

import { ContainerGridCard } from './ContainerGridCard'
import type { ContainerRow } from './types'

afterEach(() => {
  cleanup()
})

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  const rootRoute = createRootRoute({
    component: () => <Outlet />,
  })
  const childRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => ui,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([childRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('ContainerGridCard', () => {
  it('shows empty state when containers is empty', async () => {
    renderWithQueryClient(<ContainerGridCard containers={[]} />)
    const mobile = await screen.findByTestId('containers-mobile')
    expect(mobile).toHaveTextContent('No containers discovered yet.')
  })

  it('renders compose basename when compose_file_path is set', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml',
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByText('Compose:', { exact: false })).toHaveTextContent('compose')
  })

  it('renders dash for compose when compose_file_path is null', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: null,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    const composeDiv = await screen.findByText('Compose:', { exact: false })
    expect(composeDiv).toHaveTextContent('—')
  })

  it('shows full compose_file_path as tooltip', async () => {
    const filePath = '/storage/programs/homelab-monitor/deploy/compose/docker-compose.yml'
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: filePath,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    const composeDivs = await screen.findAllByText('Compose:', { exact: false })
    expect(composeDivs[0]).toBeDefined()
    const composeDiv = composeDivs[0]
    expect(composeDiv).toHaveAttribute('title', filePath)
  })

  it('renders restart_count_24h when present and > 0', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: 3,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByText('Restarts (24h):', { exact: false })).toBeInTheDocument()
    const restartDiv = screen.getByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('3')
  })

  it('renders dash for restart_count_24h when 0', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: 0,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    const restartDiv = await screen.findByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('—')
  })

  it('renders dash for restart_count_24h when null', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 5,
        restart_count_24h: null,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    const restartDiv = await screen.findByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveTextContent('—')
  })

  it('shows cumulative restart_count as tooltip', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        restart_count: 7,
        restart_count_24h: 2,
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    const restartDiv = await screen.findByText('Restarts (24h):', { exact: false })
    expect(restartDiv).toHaveAttribute('title', 'Cumulative: 7')
  })

  it('renders compose basename from nested path correctly', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-container',
        compose_file_path: '/a/b/c/docker-compose.yml',
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByText(/c$/)).toBeInTheDocument()
  })

  it('renders multiple containers', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'nginx',
        compose_file_path: '/a/b/compose/docker-compose.yml',
        labels: {},
      },
      {
        id: 'test-456',
        name: 'postgres',
        compose_file_path: '/x/y/compose/docker-compose.yml',
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByText('nginx')).toBeInTheDocument()
    expect(screen.getByText('postgres')).toBeInTheDocument()
  })

  it('renders container name', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'my-service',
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByText('my-service')).toBeInTheDocument()
  })

  it('Mobile card renders Logs → View logs link', async () => {
    const containers: ContainerRow[] = [
      {
        id: 'test-123',
        name: 'caddy',
        labels: {},
      },
    ]
    renderWithQueryClient(<ContainerGridCard containers={containers} />)
    expect(await screen.findByTestId('logs-link-mobile-caddy')).toBeInTheDocument()
  })
})
