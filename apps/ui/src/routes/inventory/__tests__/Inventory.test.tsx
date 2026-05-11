import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
} from '@tanstack/react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { InventoryLayout } from '@/routes/inventory/Inventory'

afterEach(cleanup)

function renderInventory(initialPath = '/inventory/crons') {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const inventoryRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/inventory',
    component: InventoryLayout,
  })
  const cronsRoute = createRoute({
    getParentRoute: () => inventoryRoute,
    path: '/crons',
    component: () => <div>Crons content</div>,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([inventoryRoute.addChildren([cronsRoute])]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  })
  return render(<RouterProvider router={router} />)
}

describe('InventoryLayout', () => {
  it('renders the Inventory heading', async () => {
    renderInventory()
    expect(await screen.findByRole('heading', { name: /Inventory/i })).toBeInTheDocument()
  })

  it('renders the Crons tab link', async () => {
    renderInventory()
    expect(await screen.findByRole('link', { name: /Crons/i })).toBeInTheDocument()
  })

  it('renders the nav landmark', async () => {
    renderInventory()
    expect(
      await screen.findByRole('navigation', { name: /Inventory sections/i }),
    ).toBeInTheDocument()
  })

  it('renders outlet content for the crons route', async () => {
    renderInventory('/inventory/crons')
    expect(await screen.findByText('Crons content')).toBeInTheDocument()
  })
})
