// Project test conventions:
// - Vitest (explicit imports), afterEach cleanup, render from @testing-library/react
// - Router wrapper: createRootRoute + createRoute + createRouter + RouterProvider
//   from @tanstack/react-router (mirrors CronDetailPage.test.tsx pattern)

import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { SettingsLayout } from '@/routes/settings/SettingsLayout'

afterEach(() => {
  cleanup()
})

function renderSettingsLayout() {
  const rootRoute = createRootRoute({ component: () => <Outlet /> })
  const settingsRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/settings',
    component: SettingsLayout,
  })
  const router = createRouter({
    routeTree: rootRoute.addChildren([settingsRoute]),
    history: createMemoryHistory({ initialEntries: ['/settings'] }),
  })
  render(<RouterProvider router={router} />)
}

describe('SettingsLayout', () => {
  it('renders the Settings heading', async () => {
    renderSettingsLayout()
    expect(await screen.findByText('Settings')).toBeInTheDocument()
  })

  it('renders the subtitle describing current capabilities', async () => {
    renderSettingsLayout()
    expect(await screen.findByText(/Logs retention and disk budget today/)).toBeInTheDocument()
  })
})
