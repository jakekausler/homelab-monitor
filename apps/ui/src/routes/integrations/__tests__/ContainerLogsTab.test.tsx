import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ContainerLogsTab } from '../ContainerLogsTab'

vi.mock('../DockerContainerLogsViewerBody', () => ({
  DockerContainerLogsViewerBody: () => <div>Logs viewer body</div>,
}))

function renderWithRouter(containerName: string = 'test-container') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const integrationsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/integrations',
    component: () => <Outlet />,
  })
  const dockerRoute = createRoute({
    getParentRoute: () => integrationsRoute,
    path: '/docker',
    component: () => <Outlet />,
  })
  const containersRoute = createRoute({
    getParentRoute: () => dockerRoute,
    path: '/containers',
    component: () => <Outlet />,
  })
  const containerDetailRoute = createRoute({
    getParentRoute: () => containersRoute,
    path: '/$name',
    component: () => <Outlet />,
  })
  const containerLogsRoute = createRoute({
    getParentRoute: () => containerDetailRoute,
    path: '/logs',
    component: ContainerLogsTab,
    validateSearch: (
      search: Record<string, unknown>,
    ): { since?: string | undefined; start?: string | undefined; end?: string | undefined } => ({
      since: typeof search.since === 'string' ? search.since : undefined,
      start: typeof search.start === 'string' ? search.start : undefined,
      end: typeof search.end === 'string' ? search.end : undefined,
    }),
  })

  const router = createRouter({
    routeTree: rootRoute.addChildren([
      integrationsRoute.addChildren([
        dockerRoute.addChildren([
          containersRoute.addChildren([containerDetailRoute.addChildren([containerLogsRoute])]),
        ]),
      ]),
    ]),
    history: createMemoryHistory({
      initialEntries: [`/integrations/docker/containers/${containerName}/logs`],
    }),
  })

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('ContainerLogsTab', () => {
  it('renders logs viewer body', async () => {
    renderWithRouter()
    expect(await screen.findByText('Logs viewer body')).toBeInTheDocument()
  })
})
